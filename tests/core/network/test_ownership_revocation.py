"""Who may run a session, and what a revocation does to a member's streams.

Three in-process workers (``harness.Worker``) run the real engine, catalog,
handoff and control code. Every scenario asserts what each worker ends up
holding: the session's owner, which worker accepts a forwarded turn, what is
in its conflicts list, and which of a revoked device's operations applied.

Tests named ``*_guard_bites`` run a scenario with one guard switched off (by
monkeypatch, never by editing production code) and require it to fail, so
the scenario is known to depend on that guard.
"""
import json

import httpx
import pytest

from app.core import turn_manager
from app.db import database
from app.db import network as store
from app.db import runs
from app.db import session_events as events_db
from app.db.base import get_db
from app.network import catalog, control, crypto, engine, handoff, membership, ownership, transport
from tests.core.network.harness import converge, exchange, join, owner_pref, set_owner_pref
from tests.core.network.test_transport import ServedBy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _network(workers, *names):
    made = [workers(name) for name in names]
    with made[0].active():
        engine.create_network("Ownership", device_name=made[0].name.upper())
    for other in made[1:]:
        join(made[0], other)
    converge(made)
    return made


def _session(worker, session_id, *, title="Session", events=2):
    with worker.active():
        database.create_session(session_id=session_id, profile_id="default", project_id=None, title=title,
                                is_active=True)
        if events:
            events_db.append_events(session_id, [
                {"type": "message.user", "payload": {"type": "message.user", "content": f"hello {i} from {worker.name}"}}
                for i in range(events)])
        engine.scan_all()


def _owner(worker, session_id):
    with worker.active():
        row = database.get_session(session_id)
        return row["owner_device_id"] if row else None


def _turn_sink(monkeypatch):
    """Record turns an owner enqueues instead of running an agent."""
    submitted = []

    async def fake_submit(session_id, source, **kwargs):
        submitted.append({"session_id": session_id, "source": source, **kwargs})
        return f"turn_{len(submitted)}"

    monkeypatch.setattr(turn_manager, "submit_async", fake_submit)
    return submitted


async def _forward_turn(owner_side, session_id, from_device):
    """What ``/peer/control/turns`` runs on ``owner_side`` for an authenticated member."""
    with owner_side.active():
        return await control.handle("turns", {"session_id": session_id, "prompt": "go", "source": "user"},
                                    from_device=from_device)


def _sessions_conflicts(worker, session_id):
    with worker.active():
        return [c for c in store.list_conflicts() if c["collection"] == "sessions" and c["record_id"] == session_id]


class Network:
    """Route httpx calls to several in-process workers by base URL."""

    def __init__(self, monkeypatch, **servers):
        self.served = {name: ServedBy(worker, f"http://{name}.local:8000") for name, worker in servers.items()}
        real_async = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(self.handle)
            return real_async(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client)

    def handle(self, request):
        for served in self.served.values():
            if str(request.url).startswith(served.base_url):
                return served.handler(request)
        return httpx.Response(502, json={"detail": "unreachable"})

    def url(self, name):
        return self.served[name].base_url

    def requests(self, name):
        return self.served[name].requests


# ---------------------------------------------------------------------------
# Ownership transfer and conflicting claims
# ---------------------------------------------------------------------------

REMOTE = "https://example.test/repo.git"
COMMIT = {"branch": "main", "fingerprint": "fp"}


def _stub_git_checkout(monkeypatch):
    """Resume needs a Git project; these tests only exercise the lease and owner."""
    monkeypatch.setattr(handoff.project_resolver, "resolve_project_v2", lambda session: {"id": "proj"})
    monkeypatch.setattr(handoff.project_resolver, "_effective_branch", lambda session: "main")
    monkeypatch.setattr(handoff.project_resolver, "resolve_existing_cwd", lambda session: "/checkout")
    monkeypatch.setattr(handoff.checkout_transfer, "export_checkout", lambda cwd, branch, remote: {"branch": branch})
    monkeypatch.setattr(handoff.checkout_transfer, "fingerprint", lambda cwd: "fp")
    monkeypatch.setattr(handoff.checkout_transfer, "_branch", lambda cwd, branch: None)


async def _two_claims_one_lease(workers, monkeypatch):
    a, b, c = _network(workers, "a", "b", "c")
    submitted = _turn_sink(monkeypatch)
    _stub_git_checkout(monkeypatch)
    _session(a, "moving", title="Moves to B")
    converge([a, b, c])

    # B and C both try to take the session from its owner A. The first
    # reservation holds the session; the second is refused while it lives.
    with a.active():
        reserved = handoff.begin("moving", b.device_id, REMOTE)
        with pytest.raises(ValueError, match="busy"):
            handoff.begin("moving", c.device_id, REMOTE)
    with b.active():
        key = engine.context().keyring[reserved["key_version"]]
        token = json.loads(crypto.decrypt_payload(key, reserved["payload"], handoff._aad("moving", b.device_id)))["token"]
    with a.active():
        # C cannot commit B's reservation: the lease names its holder.
        with pytest.raises(ValueError, match="reservation expired"):
            handoff.finish("moving", c.device_id, {"phase": "commit", "token": token, **COMMIT})
        assert database.get_session("moving")["owner_device_id"] == a.device_id
        assert handoff.finish("moving", b.device_id, {"phase": "commit", "token": token, **COMMIT}) == {"committed": True}
        # A retried commit is idempotent, not a second transfer.
        assert handoff.finish("moving", b.device_id, {"phase": "commit", "token": token, **COMMIT}) == {"committed": True}
    with b.active():
        handoff._accept_owner("moving", b.device_id, COMMIT["branch"])
    converge([a, b, c])

    # Every worker agrees on the new owner.
    assert _owner(a, "moving") == _owner(b, "moving") == _owner(c, "moving") == b.device_id
    for worker, expected in ((a, b.device_id), (b, None), (c, b.device_id)):
        with worker.active():
            assert control.owner_of(database.get_session("moving")) == expected
    # The former owner refuses both a late claim and a forwarded turn.
    with a.active():
        with pytest.raises(PermissionError, match="does not own"):
            handoff.begin("moving", c.device_id, REMOTE)
        assert [r["state"] for r in a.sql("SELECT state FROM runs WHERE session_id = 'moving'")] == ["transferred"]
        assert runs.get_active_run("moving") is None
    for loser in (a, c):
        with pytest.raises(PermissionError, match="does not own"):
            await _forward_turn(loser, "moving", from_device=b.device_id)
    assert submitted == []
    assert (await _forward_turn(b, "moving", from_device=c.device_id))["turn_id"] == "turn_1"
    assert submitted[0]["payload"]["forwarded_by_device"] == c.device_id
    # Once the new owner has spoken, only it publishes the session.
    with b.active():
        database.update_session(session_id="moving", title="Renamed by B")
        assert catalog.publish_owned() >= 1
    converge([a, b, c])
    with a.active():
        database.update_session(session_id="moving", title="Stale rename on A")
        assert catalog.publish_owned() == 0
    converge([a, b, c])
    for worker in (a, b, c):
        with worker.active(), get_db() as conn:
            assert store.get_record(conn.cursor(), "sessions", "moving")["origin_device_id"] == b.device_id
    for worker in (b, c):
        with worker.active():
            assert database.get_session("moving")["title"] == "Renamed by B"


async def test_two_claims_on_one_session_get_one_lease_and_one_owner(workers, monkeypatch):
    await _two_claims_one_lease(workers, monkeypatch)


async def test_two_claims_guard_bites_without_the_owner_check(workers, monkeypatch):
    """With ``_require_owned`` disabled the former owner hands the session out again."""
    monkeypatch.setattr(control, "_require_owned", lambda session_id: database.get_session(session_id))
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        await _two_claims_one_lease(workers, monkeypatch)


async def test_partitioned_owner_claims_resolve_to_one_owner_everywhere(workers, monkeypatch):
    """A stale claim written while partitioned loses to the owner's later write;
    the loser stops accepting turns and every worker keeps the losing claim."""
    a, b, c = _network(workers, "a", "b", "c")
    submitted = _turn_sink(monkeypatch)
    _session(a, "contested", title="Original")
    converge([a, b, c])
    # Partition. B claims the session without a lease (the mirror row says B).
    with b.active(), get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id = %s WHERE id = %s", (b.device_id, "contested"))
    with b.active():
        assert catalog.publish_owned() >= 1
        assert control.owner_of(database.get_session("contested")) is None  # B believes it owns it
        claim = [o for o in store.ops_since({}) if o["collection"] == "sessions" and o["origin_device_id"] == b.device_id]
    # Meanwhile the real owner keeps working (two writes: its clock moves ahead).
    for title in ("Owner edit 1", "Owner edit 2"):
        with a.active():
            database.update_session(session_id="contested", title=title)
            catalog.publish_owned()
    with a.active():
        winner = [o for o in store.ops_since({}) if o["collection"] == "sessions" and o["record_id"] == "contested"][-1]
    assert (winner["clock"], winner["origin_device_id"]) > (claim[-1]["clock"], claim[-1]["origin_device_id"])
    # Deliver in different orders: C sees the claim first, A sees it last.
    exchange(b, c)
    exchange(a, c)
    exchange(b, a)
    converge([a, b, c])

    for worker in (a, b, c):
        with worker.active():
            row = database.get_session("contested")
            assert (row["owner_device_id"], row["title"]) == (a.device_id, "Owner edit 2")
            with get_db() as conn:
                assert store.get_record(conn.cursor(), "sessions", "contested")["origin_device_id"] == a.device_id
        assert any(c["losing_device_id"] == b.device_id and c["losing_record"]["owner_device_id"] == b.device_id
                   for c in _sessions_conflicts(worker, "contested"))
    # The loser refuses turns and publishes nothing more for the session.
    with pytest.raises(PermissionError):
        await _forward_turn(b, "contested", from_device=c.device_id)
    with b.active():
        assert catalog.publish_owned() == 0
    assert (await _forward_turn(a, "contested", from_device=c.device_id))["session_id"] == "contested"
    assert [t["session_id"] for t in submitted] == ["contested"]


def test_events_held_for_a_hole_apply_once_the_owner_stream_fills(workers):
    """A mirror holds an owner's out-of-order transcript events until the hole fills."""
    a, b = _network(workers, "a", "b")
    _session(a, "held", events=3)
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        ops = store.ops_since(marks, exclude_origin=b.device_id)
    events = [o for o in ops if o["collection"] == "session_events"]
    assert len(events) == 3
    head = [o for o in ops if o not in events[1:]]
    with b.active():
        # The later events arrive first: held, nothing materialized.
        early = engine.apply_batch(events[2:] + events[1:2])
        assert early.pending == 2 and early.applied == 0
        assert events_db.get_max_seq("held") == 0
        assert store.pending_holes()
        # Everything up to the hole: the buffered events drain in order.
        late = engine.apply_batch(head)
        assert late.applied + late.lost == len(head) + 2 and store.pending_holes() == []
        assert [e["seq"] for e in events_db.get_events("held", 0)] == [1, 2, 3]
    # A duplicate delivery of the whole page changes nothing.
    with b.active():
        again = engine.apply_batch(ops)
        assert again.applied == 0 and again.pending == 0 and again.rejected == 0
        assert [e["seq"] for e in events_db.get_events("held", 0)] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Control messages go to the owner; anyone else refuses them
# ---------------------------------------------------------------------------

def _owner_side_fakes(monkeypatch):
    calls = []

    async def submit(session_id, source, **kwargs):
        calls.append(("turn", session_id, source))
        return "turn_1"

    async def interrupt(session_id, mode, stop_background=False, turn_id=None):
        calls.append(("interrupt", session_id, mode))
        return {"id": "turn_1"}

    async def permission(**kwargs):
        calls.append(("permission", kwargs["session_id"], kwargs["decision"]))
        return {"ok": True}

    async def question(**kwargs):
        calls.append(("question", kwargs["session_id"], kwargs["answered_by_device"]))
        return True

    from app.core import permission_handler as ph_mod
    from app.core import user_question_handler as uq_mod
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr(turn_manager, "interrupt_async", interrupt)
    monkeypatch.setattr(ph_mod.permission_handler, "respond", permission)
    monkeypatch.setattr(uq_mod.user_question_handler, "respond", question)
    return calls


_CONTROL_MESSAGES = [
    ("turns", lambda sid: {"session_id": sid, "prompt": "go", "source": "user"}),
    ("interrupt", lambda sid: {"session_id": sid, "mode": "stop"}),
    ("interrupt", lambda sid: {"session_id": sid, "mode": "send_now", "turn_id": "turn_1"}),  # steer
    ("permission", lambda sid: {"session_id": sid, "request_id": "r1", "decision": "allow"}),
    ("question", lambda sid: {"session_id": sid, "request_id": "q1", "answers": {"Q": "A"}}),
    ("hydrate", lambda sid: {"session_id": sid}),
]


async def _control_routing(workers, monkeypatch):
    a, b, c = _network(workers, "a", "b", "c")
    net = Network(monkeypatch, a=a, b=b)
    calls = _owner_side_fakes(monkeypatch)
    _session(a, "on-a")
    converge([a, b, c])
    for worker, name in ((a, "a"), (b, "b")):
        with c.active():
            store.touch_member(worker.device_id, endpoints=[net.url(name)])
    with c.active():
        assert control.owner_of(database.get_session("on-a")) == a.device_id
        # Sent to B, which only mirrors the session: every action is refused
        # before any handler runs on B.
        for action, body in _CONTROL_MESSAGES:
            with pytest.raises(control.ControlRefused, match="does not own"):
                await control.call(b.device_id, action, body("on-a"))
    assert calls == []
    assert [p for m, p in net.requests("b") if "/control/" in p] == [
        f"/api/v1/network/peer/control/{action}" for action, _ in _CONTROL_MESSAGES]
    with b.active():
        assert database.get_session("on-a")["owner_device_id"] == a.device_id  # nothing created or moved
        assert b.sql("SELECT COUNT(*) AS n FROM turns")[0]["n"] == 0
    # Sent to the owner, the same messages are handled, answered as C.
    with c.active():
        for action, body in _CONTROL_MESSAGES:
            await control.call(a.device_id, action, body("on-a"))
    assert calls == [
        ("turn", "on-a", "user"), ("interrupt", "on-a", "stop"), ("interrupt", "on-a", "send_now"),
        ("permission", "on-a", "allow"), ("question", "on-a", c.device_id)]


async def test_control_messages_reach_only_the_owner(workers, monkeypatch):
    await _control_routing(workers, monkeypatch)


async def test_control_routing_guard_bites_without_the_owner_check(workers, monkeypatch):
    """With the owner check off, a mirror would run turns and stops for a session it does not own."""
    monkeypatch.setattr(control, "_require_owned", lambda session_id: database.get_session(session_id))
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        await _control_routing(workers, monkeypatch)


async def test_unknown_or_malformed_control_requests_are_refused_over_http(workers, monkeypatch):
    a, b = _network(workers, "a", "b")
    net = Network(monkeypatch, a=a)
    calls = _owner_side_fakes(monkeypatch)
    with b.active():
        store.touch_member(a.device_id, endpoints=[net.url("a")])
        with pytest.raises(control.ControlRefused, match="Unknown control action"):
            await control.call(a.device_id, "format-disk", {})
        with pytest.raises(control.ControlRefused, match="Unknown session"):
            await control.call(a.device_id, "interrupt", {"session_id": "nope", "mode": "stop"})
        with pytest.raises(control.ControlRefused, match="session.id is required"):
            await control.call(a.device_id, "sessions", {"session": {}})
    assert calls == []


# ---------------------------------------------------------------------------
# Revocation propagation
# ---------------------------------------------------------------------------

def _origin_ops(worker, origin, *, since=0):
    with worker.active():
        return [o for o in store.ops_since({origin: {"1": since}}) if o["origin_device_id"] == origin]


def _revocation_scenario(workers):
    a, b, c = _network(workers, "a", "b", "c")
    # B works; A hears about it, C does not (yet).
    _session(b, "on-b", title="B's session")
    set_owner_pref(b, "before", "honoured")
    exchange(b, a)
    honoured = len(_origin_ops(b, b.device_id))
    # B keeps writing after the point A will honour.
    set_owner_pref(b, "after", "refused")
    with b.active():
        events_db.append_events("on-b", [{"type": "message.user", "payload": {"type": "message.user", "content": "late"}}])
        catalog.publish_owned()
    b_stream = _origin_ops(b, b.device_id)
    assert len(b_stream) > honoured

    with a.active():
        outcome = engine.revoke_device(b.device_id, reason="lost laptop")
        member = engine.context().member(b.device_id)
        assert (member.status, member.last_seq_accepted, member.last_incarnation_accepted) == ("revoked", honoured, 1)
    # C learns the revocation from A's own stream before B's ops reach it.
    a_stream = _origin_ops(a, a.device_id)
    with c.active():
        learned = engine.apply_batch(a_stream)
        assert learned.rejected == 0
        assert engine.context().member(b.device_id).status == "revoked"
        assert engine.context().key_version == outcome["key_version"]
        # B's stream arrives late and whole: everything up to the revocation
        # point applies, nothing after it.
        late = engine.apply_batch(b_stream)
        assert late.applied + late.lost == honoured
        assert late.rejected == len(b_stream) - honoured and "origin revoked" in late.reasons
        with get_db() as conn:
            assert store.get_watermark(conn.cursor(), b.device_id, 1) == len(b_stream)
    # A refuses the same late ops.
    with a.active():
        refused = engine.apply_batch(b_stream)
        assert refused.applied == 0 and refused.rejected == len(b_stream) - honoured
    for worker in (a, c):
        assert owner_pref(worker, "before") == "honoured"
        assert owner_pref(worker, "after") is None
        with worker.active():
            assert database.get_session("on-b")["owner_device_id"] == b.device_id  # frozen, not re-owned
            assert events_db.get_max_seq("on-b") == 2
            assert store.pending_holes() == []
    return a, b, c


def test_revocation_honours_the_stream_up_to_its_point_on_every_member(workers):
    _revocation_scenario(workers)


def test_revocation_guard_bites_without_the_stream_limit(workers, monkeypatch):
    """Honouring every op of a revoked member lets its later writes through on C."""
    monkeypatch.setattr(engine, "_origin_allowed", lambda cursor, ctx, env: ctx.member(env.origin_device_id))
    with pytest.raises(AssertionError):
        _revocation_scenario(workers)


def test_revoked_member_learns_it_and_stops_executing(workers):
    a, b, c = _revocation_scenario(workers)
    for worker in (a, b, c):
        with worker.active():
            assert ownership.execution_eligible() is True
    exchange(a, b)
    with b.active():
        ctx = engine.context()
        assert ctx.member(b.device_id).status == "revoked"
        assert ownership.execution_eligible() is False
        assert ownership.claim("automations", "anything") is False
        # B cannot open the rotated key, so A's new writes stay unreadable to it.
        assert ctx.key_version == 1 and 2 not in ctx.keyring
    set_owner_pref(a, "secret-after-rotation", "x")
    with b.active():
        pending = [o for o in _origin_ops(a, a.device_id) if o["record_id"].endswith("secret-after-rotation")]
        assert pending and engine.apply_batch(pending).retry == 1
    assert owner_pref(b, "secret-after-rotation") is None
    converge([a, c])
    assert owner_pref(c, "secret-after-rotation") == "x"
    for worker in (a, c):
        with worker.active():
            assert ownership.execution_eligible() is True


async def test_turns_for_a_revoked_owners_session_are_not_sent_to_it(workers, monkeypatch):
    """Regression: ``control.call`` dialled a removed device, handing it the user's prompt.

    Its sessions stay frozen on the remaining members (read-only mirror, forkable)."""
    a, b, c = _revocation_scenario(workers)
    net = Network(monkeypatch, b=b)
    calls = _owner_side_fakes(monkeypatch)
    with c.active():
        store.touch_member(b.device_id, endpoints=[net.url("b")])
        assert control.owner_of(database.get_session("on-b")) == b.device_id
        for send in (lambda: control.submit_turn(b.device_id, "on-b", prompt="secret plans"),
                     lambda: control.interrupt(b.device_id, "on-b", "stop")):
            with pytest.raises(control.RunnerUnreachable, match="removed"):
                await send()
        # The mirror can still be continued locally.
        forked = control.fork_here("on-b")
        assert database.get_session(forked["session_id"])["owner_device_id"] == c.device_id
        assert "hello 0 from b" in database.get_session_messages(forked["session_id"])[0]["content"]
    assert net.requests("b") == []
    assert calls == []
    with b.active():
        assert b.sql("SELECT COUNT(*) AS n FROM turns")[0]["n"] == 0


async def test_a_revoked_device_cannot_drive_a_members_sessions(workers, monkeypatch):
    a, b, c = _revocation_scenario(workers)
    net = Network(monkeypatch, a=a)
    calls = _owner_side_fakes(monkeypatch)
    _session(a, "on-a")
    with b.active():
        # B has not heard of its revocation and still believes A will serve it.
        assert engine.context().member(a.device_id).status == "active"
        store.touch_member(a.device_id, endpoints=[net.url("a")])
        transport.reset_for_tests()
        with pytest.raises((control.RunnerUnreachable, control.ControlRefused)):
            await control.submit_turn(a.device_id, "on-a", prompt="hijack")
    assert calls == []
    assert ("POST", "/api/v1/network/peer/control/turns") not in net.requests("a")
    with a.active():
        assert b.device_id not in transport.online_devices()


async def test_sync_loop_drops_links_to_revoked_members(workers):
    a, b, c = _network(workers, "a", "b", "c")
    with a.active():
        for worker in (b, c):
            store.touch_member(worker.device_id, endpoints=[f"http://{worker.name}.local:8000"])
        await transport._refresh_links(engine.context())
        assert set(transport._links) == {b.device_id, c.device_id}
        engine.revoke_device(b.device_id)
        await transport._refresh_links(engine.context())
        assert set(transport._links) == {c.device_id}


def test_revocation_withdraws_pending_ops_and_blocks_a_new_incarnation(workers):
    a, b, c = _network(workers, "a", "b", "c")
    set_owner_pref(b, "k1", 1)
    set_owner_pref(b, "k2", 2)
    set_owner_pref(b, "k3", 3)
    stream = _origin_ops(b, b.device_id)
    with c.active():
        # Only the tail of B's stream reached C: held behind the hole.
        held = engine.apply_batch(stream[1:])
        assert held.pending == len(stream) - 1
    with a.active():
        engine.revoke_device(b.device_id)
    with c.active():
        engine.apply_batch(_origin_ops(a, a.device_id))
        assert engine.context().member(b.device_id).status == "revoked"
        # A had applied none of B's ops: nothing B buffered can ever apply.
        assert store.pending_holes() == []
        assert engine.apply_batch(stream).applied == 0
    # A fresh stream (as after a restore) from the removed device is refused too.
    with b.active():
        from app.network import identity
        identity.bump_incarnation()
        engine.load_context()
    set_owner_pref(b, "k4", 4)
    with b.active():
        fresh = [o for o in store.ops_since({}) if o["origin_device_id"] == b.device_id and o["origin_incarnation"] == 2]
    for worker in (a, c):
        with worker.active():
            result = engine.apply_batch(fresh)
            assert result.applied == 0 and "origin revoked" in result.reasons
        assert owner_pref(worker, "k4") is None and owner_pref(worker, "k1") is None


def test_exile_cannot_return_with_a_recovery_certificate_under_the_old_key(workers):
    """A removed device knows the pre-rotation key. A recovery certificate
    proven under it (for a fresh identity) must not admit anyone."""
    a, b, c = _network(workers, "a", "b", "c")
    with b.active():
        old_key = engine.context().network_key
        network_id = engine.context().network_id
    with a.active():
        engine.revoke_device(b.device_id)
    converge([a, c])
    impostor = workers("impostor")
    with impostor.active():
        ident = impostor.identity
        cert = membership.issue_recovery_certificate(
            network_id=network_id, network_key=old_key, signing_private=ident.signing_private,
            encryption_public=ident.encryption_public, name="Back again", kind="worker", generation=9)
        record_id = f"cert:{ident.device_id}:9"
        from app.network import envelope as envelope_mod
        env = envelope_mod.build(
            network_id=network_id, collection="membership", record_id=record_id, op="upsert", clock=10_000,
            origin_device_id=ident.device_id, origin_incarnation=1, origin_seq=1, schema_version=1,
            key_version=1, network_key=old_key, signing_private=ident.signing_private,
            record={"id": record_id, "record": cert, "created_at": envelope_mod.now_iso()})
    for worker in (a, c):
        with worker.active():
            result = engine.apply_batch([env.to_dict()])
            assert result.applied == 0
            assert engine.context().member(ident.device_id) is None
            assert store.get_member(ident.device_id) is None

"""Peer transport and enrollment over HTTP, with worker A served in-process.

Worker B's HTTP calls are routed by an httpx MockTransport into a
TestClient of worker A's router, entered under A's context. Everything else
is the real code: tokens, exchange, push, snapshot copy, join polling.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.api import pairing as pairing_api
from app.api.auth import require_admin, require_auth
from app.core import pairing as pairing_core
from app.db import database
from app.db import network as store
from app.network import engine, enrollment, identity, tokens, transport
from tests.core.network.harness import Worker, owner_pref, set_owner_pref


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
    enrollment._task = None


def test_liveness_expiry_and_reconnect_invalidate_without_peer_writes(monkeypatch):
    from app.core.global_events import global_events

    now = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    events = []
    monkeypatch.setattr(global_events, "resources_changed",
                        lambda resource, **kw: events.append((resource, kw["user_type"])))
    transport.reset_for_tests()
    transport.note_contact("owner")
    step = [0]

    async def tick(_delay):
        step[0] += 1
        if step[0] == 1:
            assert len(events) == 8  # first real contact, all authenticated audiences
        elif step[0] == 2:
            assert len(events) == 8  # quiet time emits nothing
            now[0] += transport.ONLINE_WINDOW_SECONDS + 1
        elif step[0] == 3:
            assert len(events) == 16  # expiry, with no incoming record
            transport.note_contact("owner")
        elif step[0] == 4:
            assert len(events) == 24  # reconnect
            transport.forget_peers()
        else:
            assert len(events) == 32  # leaving the network
            raise asyncio.CancelledError

    monkeypatch.setattr(transport.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(transport.liveness_loop())
    assert set(events) == {(r, k) for r in ("sessions", "network")
                           for k in ("admin", "api_user", "account", "paired")}


def _app():
    app = FastAPI()
    app.include_router(network_api.router)
    app.include_router(pairing_api.router)
    from app.middleware.csrf import CSRFProtectionMiddleware
    app.add_middleware(CSRFProtectionMiddleware)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = lambda: "admin"
    return app


class ServedBy:
    """An httpx transport that answers as worker ``server`` for a base URL."""

    def __init__(self, server: Worker, base_url: str):
        self.server = server
        self.base_url = base_url.rstrip("/")
        self.client = TestClient(_app())
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not url.startswith(self.base_url):
            return httpx.Response(502, json={"detail": "unreachable"})
        path = url[len(self.base_url):]
        self.requests.append((request.method, path.split("?")[0]))
        with self.server.active():
            response = self.client.request(request.method, path, content=request.content,
                                           headers={k: v for k, v in request.headers.items()
                                                    if k.lower() in ("authorization", "content-type", "accept")})
            if (path.split("?")[0] == "/api/v1/network/peer/state" and response.status_code == 200
                    and response.json().get("status") == "preparing"):
                # This synchronous mock swaps process-wide identity/path globals
                # between workers. Drain the server job before restoring them;
                # background timing is tested with a single active server below.
                from app.network import peer_snapshot
                recipient = tokens.verify_token(request.headers["authorization"].removeprefix("Bearer "))
                ctx = engine.require_context()
                fmt = peer_snapshot.requested_format(request.url.params.get("format", 1))
                job = peer_snapshot._jobs[(str(self.server.data_dir), recipient, ctx.network_id, ctx.generation, fmt)]
                try:
                    job.result()
                except ValueError:
                    pass  # Leave the failure for the next poll to expose through the API.
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": response.headers.get("content-type", "application/json")})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


async def test_peer_link_exchanges_both_ways_and_detects_rollback(workers, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "cors_origins", "https://app.example.com")
    a, b = workers("a"), workers("b")
    served = ServedBy(a, "https://a.example.com")
    with a.active():
        engine.create_network("Home", device_name="A")
        engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                            encryption_public=b.identity.encryption_public, name="B")
        response = engine.join_response(device_id=b.device_id, encryption_public=b.identity.encryption_public,
                                        certificate=[r["record"] for r in store.list_membership_records()
                                                     if r["record"].get("device_id") == b.device_id][0])
        manifest = engine.snapshot_manifest()
        chunks = [engine.snapshot_chunk(c, 0) for c in manifest["collections"]]
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
    with b.active():
        engine.enroll_from_response(response, device_name="B")
        for chunk in chunks:
            engine.apply_snapshot_chunk(chunk)
        engine.adopt_watermarks(manifest["watermarks"])
        engine.finish_bootstrap()
    set_owner_pref(b, "from_b", 1)

    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, ["http://172.17.0.11:8000", "https://a.example.com"])
            import threading
            from unittest.mock import patch
            loop_thread = threading.get_ident()
            describe = transport._description

            def describe_off_loop():
                assert threading.get_ident() != loop_thread
                return describe()

            with patch.object(transport, "_description", side_effect=describe_off_loop) as descriptions:
                summary = await link.sync_once(client, engine.context())
            assert descriptions.call_count == 2  # authentication and exchange (a LAN probe sends none)
            assert summary["received"] >= 1 and summary["sent"] >= 1
            assert link.base_url == "https://a.example.com" and link.token
            token_before = link.token
            # Second pass reuses the token and moves nothing. Count real head
            # queries across both workers, not a seeded summary of a quiet pass.
            with patch.object(store, "local_watermarks", wraps=store.local_watermarks) as heads:
                summary = await link.sync_once(client, engine.context())
            assert summary["received"] == 0 and summary["sent"] == 0 and link.token == token_before
            assert heads.call_count == 3  # pull frontier, server response, push frontier
    assert owner_pref(a, "from_b") == 1
    with b.active():
        assert database.get_system_setting("tts_model") == "voice-a"
        assert store.list_peers()[0]["device_id"] if store.list_peers() else True
    with a.active():
        assert b.device_id in transport.online_devices()
        member = [m for m in store.list_members() if m["device_id"] == b.device_id][0]
        assert isinstance(member["endpoints"], list)

    # Restore-from-backup: wipe B's own ops and watch the incarnation bump.
    with b.active():
        b.sql("DELETE FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))
        assert b.identity.incarnation == 1
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, ["https://a.example.com"])
            await link.sync_once(client, engine.context())
            assert identity.load().incarnation == 2
            assert engine.context().identity.incarnation == 2
    set_owner_pref(b, "after_restore", True)
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, ["https://a.example.com"])
            link.token = None
            summary = await link.sync_once(client, engine.context())
            assert summary["sent"] >= 1
    assert owner_pref(a, "after_restore") is True
    with a.active():
        marks = store.local_watermarks()
        assert "2" in marks[b.device_id]


async def test_rollback_proof_survives_peer_pruning(workers):
    from tests.core.network.harness import exchange, join
    a, b = workers("proof-holder"), workers("restored-writer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    for value in range(4):
        set_owner_pref(b, "before-restore", value)
    exchange(b, a)
    with a.active():
        assert store.prune_ops(keep_days=-1) > 0
        heads = store.local_watermarks()
        with store.get_db() as conn:
            assert store.get_op(conn.cursor(), b.device_id, 1, 1) is None
    with b.active():
        b.sql("DELETE FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))
    served = ServedBy(a, "https://proof-holder.example.com")
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            original_context = engine.context()
            await link.sync_once(client, original_context, catch_up=True)
            assert engine.context().identity.incarnation == 2
            # A concurrent startup link can still hold the old context and
            # the same signed proof after the first recovery completes.
            await link._check_rollback(client, original_context, heads)
            assert engine.context().identity.incarnation == 2
        set_owner_pref(b, "after-restore", True)
        with b.active():
            await link.sync_once(client, engine.context())
    assert owner_pref(a, "before-restore") == 3
    assert owner_pref(a, "after-restore") is True


async def test_mirror_freshness_tracks_real_backlog_until_committed(workers, monkeypatch):
    from tests.core.network.harness import join
    a, b = workers("owner"), workers("mirror")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    for index in range(4):
        set_owner_pref(a, f"backlog-{index}", index)
    served = ServedBy(a, "https://owner.example.com")
    monkeypatch.setattr(transport, "BATCH_LIMIT", 1)
    monkeypatch.setattr(transport, "SYNC_PULL_SECONDS", 0)
    observed = []
    apply = engine.apply_batch

    def applying(ops):
        # The transport has observed the owner's real frontier before applying.
        observed.append(transport.owner_sync_status(a.device_id))
        return apply(ops)

    monkeypatch.setattr(engine, "apply_batch", applying)
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context())
            behind = transport.owner_sync_status(a.device_id)
            assert behind["state"] == "syncing"
            assert behind["pending_updates"] > 0
            # Unreachable/expired contacts must not clear known missing updates.
            transport._last_contact.clear()
            assert transport.owner_sync_status(a.device_id) == behind
            assert transport.owner_sync_status(b.device_id)["state"] == "current"
            await link.sync_once(client, engine.context(), catch_up=True)
            assert transport.owner_sync_status(a.device_id) == {"state": "current", "pending_updates": 0}
    assert observed[0]["state"] == "syncing"
    assert owner_pref(b, "backlog-3") == 3


async def test_session_freshness_ignores_other_chats_and_tracks_all_events(workers):
    from app.db import session_events
    from app.network import catalog
    from tests.core.network.harness import join

    a, b = workers("scoped-owner"), workers("scoped-mirror")
    with a.active():
        engine.create_network("Home")
        database.create_session("viewed", "default", title="Viewed")
        session_events.append_events("viewed", [{"type": "message.user", "payload": {"content": "hello"}}])
        catalog.publish_owned()
    join(a, b)
    served = ServedBy(a, "https://scoped-owner.example.com")
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            # First poll subscribes; old peers/unknown per-session heads fall back.
            assert transport.session_sync_status("viewed", a.device_id)["scope"] == "owner"
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context())
            current = transport.session_sync_status("viewed", a.device_id)
            assert current["state"] == "current"
            assert current["scope"] == "session"
            assert current["seq"] == current["owner_seq"] == 1
            assert current["epoch"] == current["owner_epoch"] == session_events.get_log_epoch("viewed")
        with a.active():
            database.create_session("unrelated", "default", title="Unrelated")
            session_events.append_events("unrelated", [{"type": "message.user", "payload": {"content": "busy"}}])
            # Unpublished other-chat events set the owner-wide catalog backlog.
            engine.scan_collection("sessions", owner_only=True)
        with b.active():
            await link.sync_once(client, engine.context())
            assert transport.owner_sync_status(a.device_id)["state"] == "syncing"
            transport._snapshot_peers.add(a.device_id)
            assert transport.session_sync_status("viewed", a.device_id) == current
            transport._snapshot_peers.clear()
        with a.active():
            # User revision is unchanged: freshness must include non-user events.
            session_events.append_events("viewed", [
                {"type": "subagent.progress", "payload": {"text": "working"}},
                {"type": "message.assistant", "payload": {"content": "done"}},
            ])
            assert database.get_session("viewed")["last_user_message_seq"] == 1
        with b.active():
            await link.sync_once(client, engine.context())
            behind = transport.session_sync_status("viewed", a.device_id)
            assert behind["state"] == "syncing"
            assert behind["pending_updates"] == 2
            assert behind["owner_seq"] == 3
            # Failure/offline cannot discard an observed missing transcript tail.
            transport._last_contact.clear()
            assert transport.session_sync_status("viewed", a.device_id) == behind
        with a.active():
            catalog.publish_events(session_ids=["viewed"])
        with b.active():
            await link.sync_once(client, engine.context())
            assert transport.session_sync_status("viewed", a.device_id)["state"] == "current"
            assert transport.session_sync_status("viewed", a.device_id)["pending_updates"] == 0
            # Other-chat catalog backlog is still present.
            assert transport.owner_sync_status(a.device_id)["state"] == "syncing"


@pytest.mark.parametrize("reseed", [False, True])
async def test_session_freshness_tracks_clear_epoch_even_when_seq_drops(workers, reseed):
    from app.db import session_events
    from app.db.sessions import clear_session_conversation
    from app.network import catalog
    from tests.core.network.harness import join

    a, b = workers("clear-owner"), workers("clear-mirror")
    with a.active():
        engine.create_network("Home")
        database.create_session("cleared", "default", title="Cleared")
        session_events.append_events("cleared", [
            {"type": "message.user", "payload": {"content": str(i)}} for i in range(3)
        ])
        catalog.publish_owned()
    join(a, b)
    served = ServedBy(a, "https://clear-owner.example.com")
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            transport.session_sync_status("cleared", a.device_id)
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context())
            assert transport.session_sync_status("cleared", a.device_id)["state"] == "current"
        with a.active():
            clear_session_conversation("cleared")
            if reseed:
                session_events.append_events("cleared", [{"type": "message.user", "payload": {"content": "new"}}])
        with b.active():
            await link.sync_once(client, engine.context())
            behind = transport.session_sync_status("cleared", a.device_id)
            assert behind["state"] == "syncing"
            assert behind["epoch"] != behind["owner_epoch"]
            assert behind["seq"] == 3
            assert behind["pending_updates"] == int(reseed)
        with a.active():
            catalog.publish_owned()
        with b.active():
            await link.sync_once(client, engine.context())
            status = transport.session_sync_status("cleared", a.device_id)
            assert status["state"] == "current"
            assert status["seq"] == status["owner_seq"] == int(reseed)
            assert status["epoch"] == status["owner_epoch"]


async def test_session_freshness_waits_for_multibatch_epoch_replacement(workers, monkeypatch):
    from app.db import session_events
    from app.db.sessions import clear_session_conversation
    from app.network import catalog
    from tests.core.network.harness import join

    a, b = workers("replacement-owner"), workers("replacement-mirror")
    with a.active():
        engine.create_network("Home")
        database.create_session("replaced", "default", title="Replaced")
        session_events.append_events("replaced", [
            {"type": "message.user", "payload": {"content": f"old-{i}"}} for i in range(502)
        ])
        catalog.publish_owned()
    join(a, b)
    served = ServedBy(a, "https://replacement-owner.example.com")
    monkeypatch.setattr(transport, "BATCH_LIMIT", 500)
    monkeypatch.setattr(transport, "SYNC_PULL_SECONDS", 0)
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            transport.session_sync_status("replaced", a.device_id)
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context())
            assert transport.session_sync_status("replaced", a.device_id)["state"] == "current"
        with a.active():
            clear_session_conversation("replaced")
            session_events.append_events("replaced", [
                {"type": "message.user", "payload": {"content": f"new-{i}"}} for i in range(501)
            ])
            catalog.publish_owned()
        with b.active():
            await link.sync_once(client, engine.context())
            rows = session_events.get_events("replaced", limit=1000)
            assert any(row["payload"]["content"].startswith("old-") for row in rows)
            behind = transport.session_sync_status("replaced", a.device_id)
            # The first row is new, but max(seq) still belongs to the old tail.
            assert behind["epoch"] == behind["owner_epoch"]
            assert behind["seq"] == behind["owner_seq"] == 501
            assert behind["state"] == "syncing"
            assert behind["pending_updates"] > 0
            transport._last_contact.clear()
            assert transport.session_sync_status("replaced", a.device_id) == behind
            # Even losing the in-memory witness cannot produce a false current
            # when this partial copy is revisited on the next exchange.
            transport.forget_peers()
            transport.session_sync_status("replaced", a.device_id)
            await link.sync_once(client, engine.context())
            rows = session_events.get_events("replaced", limit=1000)
            assert all(row["payload"]["content"].startswith("new-") for row in rows)
            status = transport.session_sync_status("replaced", a.device_id)
            assert status["state"] == "current"
            assert status["pending_updates"] == 0


async def test_session_freshness_without_local_copy_uses_owner_backlog(workers):
    from app.db import session_events
    from app.network import catalog
    from tests.core.network.harness import join

    a, b = workers("missing-owner"), workers("missing-mirror")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        database.create_session("not-copied", "default", title="Not copied")
        session_events.append_events("not-copied", [{"type": "message.user", "payload": {"content": "hello"}}])
        # Do not publish the row: exchange has an authoritative head but no local copy.
        assert catalog.transcript_watermarks(["not-copied"], ctx=engine.context())
    served = ServedBy(a, "https://missing-owner.example.com")
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            transport.session_sync_status("not-copied", a.device_id)
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context())
            assert session_events.get_watermark("not-copied") is None
            assert transport.session_sync_status("not-copied", a.device_id) == {
                **transport.owner_sync_status(a.device_id), "scope": "owner",
            }
            assert transport.session_sync_status("not-copied", a.device_id)["state"] == "syncing"


def test_session_freshness_checks_witness_at_owner_seq_not_newer_local_tail(workers):
    from app.db import session_events

    worker = workers("ahead-mirror")
    with worker.active():
        engine.create_network("Home")
        database.create_session("viewed", "default", title="Viewed")
        session_events.append_events("viewed", [{"type": "message.user", "payload": {"content": "one"}}])
        head = session_events.get_watermark("viewed")
        transport.session_sync_status("viewed", "owner")
        transport.note_session_watermarks("owner", ["viewed"], {"viewed": head})
        session_events.append_events("viewed", [{"type": "message.user", "payload": {"content": "two"}}])
        status = transport.session_sync_status("viewed", "owner")
        assert status["seq"] == 2
        assert status["owner_seq"] == 1
        assert status["state"] == "current"
        assert status["pending_updates"] == 0
        assert "tail_id" not in status  # witness stays on the peer protocol


def test_session_watermark_interest_is_bounded_expires_and_resets(workers, monkeypatch):
    worker = workers("interest")
    now = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(transport, "SESSION_WATERMARK_LIMIT", 2)
    with worker.active():
        engine.create_network("Home")
        for session_id in ["one", "two", "three"]:
            transport.session_sync_status(session_id, "remote-owner")
            now[0] += 1
        assert transport.requested_session_watermarks("remote-owner") == ["two", "three"]
        transport.note_session_watermarks("remote-owner", ["two"], {"two": {"seq": 4, "epoch": "e", "tail_id": "t"}})
        assert transport._session_watermarks
        now[0] += transport.SESSION_WATERMARK_TTL
        assert transport.requested_session_watermarks("remote-owner") == []
        assert not transport._session_watermarks
        transport.session_sync_status("one", "remote-owner")
        transport.forget_peers()
        assert transport.requested_session_watermarks("remote-owner") == []


@pytest.mark.parametrize("heads", [None, {}, {"viewed": {"seq": -1, "epoch": "e"}},
                                   {"viewed": {"seq": 2, "epoch": "e"}}])
def test_session_freshness_unavailable_peer_head_falls_back(workers, heads):
    worker = workers("old-peer-mirror")
    with worker.active():
        engine.create_network("Home")
        database.create_session("viewed", "default", title="Viewed")
        transport.note_watermarks("owner", {"owner": {"1": 12}})
        transport.session_sync_status("viewed", "owner")
        transport.note_session_watermarks("owner", ["viewed"], {"viewed": {"seq": 2, "epoch": "e", "tail_id": "t"}})
        transport.note_session_watermarks("owner", ["viewed"], heads)
        assert transport.session_sync_status("viewed", "owner") == {
            "state": "syncing", "pending_updates": 12, "scope": "owner",
        }


async def test_mirror_waits_for_unpublished_transcript_not_just_op_watermarks(workers):
    from app.db import session_events
    from app.network import catalog
    from tests.core.network.harness import join
    a, b = workers("event-owner"), workers("event-mirror")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        database.create_session("backlogged-chat", "default", title="Backlogged")
        session_events.append_events("backlogged-chat", [
            {"type": "message.user", "payload": {"content": f"turn-{i}"}} for i in range(3)
        ])
        # Commit just the row and first event, as a budget-limited scanner does.
        engine.scan_collection("sessions", owner_only=True)
        catalog.publish_events(session_ids=["backlogged-chat"], limit=1)
        assert catalog.has_events_backlog()
    served = ServedBy(a, "https://event-owner.example.com")
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context())
            # All published ops arrived, but the source has unpublished events.
            assert transport.owner_sync_status(a.device_id) == {"state": "syncing", "pending_updates": 0}
            assert len(session_events.get_events("backlogged-chat")) == 1
        with a.active():
            catalog.publish_events(session_ids=["backlogged-chat"])
            assert not catalog.has_events_backlog()
        with b.active():
            await link.sync_once(client, engine.context())
            assert transport.owner_sync_status(a.device_id) == {"state": "current", "pending_updates": 0}
            assert len(session_events.get_events("backlogged-chat")) == 3


@pytest.mark.parametrize("replace_all", [False, True])
async def test_rollback_proof_after_cross_origin_head_pruning(workers, replace_all):
    from tests.core.network.harness import exchange, join
    a, b = workers("proof-holder"), workers("restored-writer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    set_owner_pref(b, "record-x", "b1")
    set_owner_pref(b, "record-y", "b2")
    exchange(b, a)
    with b.active():
        with store.get_db() as conn:
            head = store.own_watermark(conn.cursor(), b.device_id, 1)
    set_owner_pref(a, "record-y", "a-replacement")
    if replace_all:
        set_owner_pref(a, "record-x", "a-replacement")
    with a.active():
        assert store.prune_ops(keep_days=-1) > 0
        assert store.local_watermarks()[b.device_id]["1"] == head
        with store.get_db() as conn:
            assert store.get_op(conn.cursor(), b.device_id, 1, head) is None
        retained = a.sql("SELECT seq FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))
        assert bool(retained) is not replace_all
    with b.active():
        b.sql("DELETE FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))
    served = ServedBy(a, "https://proof-holder.example.com")
    async with httpx.AsyncClient(transport=served.transport()) as client:
        denied = await client.get(f"{served.base_url}/api/v1/network/peer/ops/{b.device_id}/1/1",
                                  params={"at_or_after": "true"})
        assert denied.status_code == 401
        served.requests.clear()
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            await asyncio.wait_for(link.sync_once(client, engine.context(), catch_up=True), timeout=5)
            assert engine.context().identity.incarnation == (1 if replace_all else 2)
            proofs = [r for r in served.requests if r[0] == "GET" and "/peer/ops/" in r[1]]
            assert len(proofs) == 1
            served.requests.clear()
            await asyncio.wait_for(link.sync_once(client, engine.context(), catch_up=True), timeout=5)
            proofs = [r for r in served.requests if r[0] == "GET" and "/peer/ops/" in r[1]]
            assert len(proofs) == (1 if replace_all else 0)
        if not replace_all:
            set_owner_pref(b, "after-restore", True)
            with b.active():
                await link.sync_once(client, engine.context())
            assert owner_pref(a, "after-restore") is True
    assert owner_pref(a, "record-y") == "a-replacement"


async def test_enrollment_over_http_then_sync(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = ServedBy(a, "http://a.local:8000")
    monkeypatch.setattr(enrollment, "POLL_INTERVAL_SECONDS", 0.01)

    real_async, real_sync = httpx.AsyncClient, httpx.Client

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    def sync_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_sync(**kwargs)

    # enrollment uses the module, so patch the module attributes for its calls only.
    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", sync_client)

    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
        grant = pairing_core.create_grant(endpoint_url="http://a.local:8000", purpose="network:join")
        code = grant["code"]
        fingerprint = a.identity.fingerprint
        wrong_fp = fingerprint[:-4] + ("0000" if not fingerprint.endswith("0000") else "1111")

    with b.active():
        with pytest.raises(enrollment.EnrollmentError):
            await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=code, inviter_fingerprint=wrong_fp)
        started = await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=code,
                                                    inviter_fingerprint=fingerprint, device_name="B")
        assert started["status"] == "pending" and started["inviter"]["device_id"] == a.device_id
        assert enrollment.enrollment_status()["status"] in ("pending", "syncing", "complete")
        poller = enrollment._task
    # The one-time code admitted B on contact: nothing waits for an administrator on A.
    with a.active():
        assert enrollment.pending_joins() == []
        assert engine.context().member(b.device_id).status == "active"

    with b.active():
        await asyncio.wait_for(poller, timeout=10)
        status = enrollment.enrollment_status()
        assert status["status"] == "complete", status
        ctx = engine.context()
        assert ctx is not None and ctx.bootstrap_state == "ready"
        assert database.get_system_setting("tts_model") == "voice-a"
        assert ctx.member(a.device_id).name == "A"
        assert [m["endpoints"][0] for m in store.list_members() if m["device_id"] == a.device_id] == ["http://a.local:8000"]
    paths = [p for _, p in served.requests]
    assert "/api/v1/network/join" in paths and "/api/v1/network/peer/state" in paths

    # B now syncs to A over the transport loop's link.
    set_owner_pref(b, "from_b", 42)
    async with real_async(transport=served.transport()) as client:
        with b.active():
            results = await transport.sync_pass(client)
            assert results[a.device_id]["sent"] >= 1
    assert owner_pref(a, "from_b") == 42


async def test_peer_loop_quiets_after_apply_and_wakes_for_a_real_write(workers, monkeypatch):
    from types import SimpleNamespace
    from app.network import blobs
    from tests.core.network.harness import join

    a, b = workers("loop-a"), workers("loop-b")
    with a.active():
        engine.create_network("Loop pacing")
    join(a, b)
    served = ServedBy(a, "http://loop-a.local:8000")
    set_owner_pref(b, "first-write", 1)
    timeouts = []
    clients = []
    real_client = httpx.AsyncClient

    def client(**kwargs):
        result = real_client(transport=served.transport(), **kwargs)
        clients.append(result)
        return result

    async def wait_for(coro, *, timeout):
        coro.close()
        timeouts.append(timeout)
        if len(timeouts) == 2:
            # The real mutation and scanner publish through engine.kick.
            database.set_system_setting("tts_model", "wake-the-peer")
            engine.scan_all()
            await asyncio.sleep(0)  # deliver call_soon_threadsafe's wake
            assert transport._wake.is_set()
        if len(timeouts) == 4:
            raise asyncio.CancelledError
        raise asyncio.TimeoutError

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(transport, "_loop", asyncio.get_running_loop())
    monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
        Event=asyncio.Event, Lock=asyncio.Lock, CancelledError=asyncio.CancelledError,
        TimeoutError=asyncio.TimeoutError, to_thread=asyncio.to_thread, wait_for=wait_for))
    # No blobs are seeded: use the real fetcher, which must report no activity.
    with b.active():
        assert await blobs.fetch_missing(limit=4) == 0
        store.touch_member(a.device_id, endpoints=[served.base_url])
        with pytest.raises(asyncio.CancelledError):
            await transport.sync_loop()
        await transport.stop()
        assert engine._kick is None
    # Each push landed completely, so no pass needs a quick follow-up. A pass
    # that pushed waits the base idle time; a quiet one after it waits twice that.
    idle = transport.SYNC_IDLE_SECONDS
    assert timeouts == [idle, 2 * idle, idle, 2 * idle]
    assert clients and all(c.is_closed for c in clients)
    assert [path for _, path in served.requests].count("/api/v1/network/peer/ops") == 2
    with a.active():
        assert database.get_system_setting("tts_model") == "wake-the-peer"
        assert engine.scan_all() == 0  # incoming records are not republished


async def test_discovery_reads_off_loop_and_keeps_links_on_loop(workers, monkeypatch):
    import threading
    worker = workers("discovery")
    loop_thread = threading.get_ident()
    presence, members = engine.all_presence, store.list_members
    reads = []

    def read_presence():
        reads.append(threading.get_ident())
        return presence()

    def read_members(**kwargs):
        reads.append(threading.get_ident())
        return members(**kwargs)

    with worker.active():
        engine.create_network("Discovery")
        monkeypatch.setattr(engine, "all_presence", read_presence)
        monkeypatch.setattr(store, "list_members", read_members)
        await transport._refresh_links(engine.context())
    assert len(reads) == 2 and all(thread != loop_thread for thread in reads)
    assert transport._links == {}


async def test_enrolled_worker_switches_networks_with_an_invite(workers, monkeypatch):
    """A clean-install worker later joins another worker: it leaves its own network
    only once the inviter accepted the code; the inviter's records win, local-only rows merge."""
    a, c = workers("a"), workers("c")
    served = ServedBy(a, "http://a.local:8000")
    monkeypatch.setattr(enrollment, "POLL_INTERVAL_SECONDS", 0.01)
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: real_async(**{**kwargs, "transport": served.transport()}))
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_sync(**{**kwargs, "transport": served.transport()}))
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
        grant = pairing_core.create_grant(endpoint_url="http://a.local:8000", purpose="network:join")
    with c.active():
        engine.create_network("Solo", device_name="C")
        database.set_system_setting("tts_model", "voice-c")
        database.set_system_setting("only_on_c", "yes")
        engine.scan_all()
        solo = engine.context().network_id
        with pytest.raises(enrollment.EnrollmentError, match="already belongs"):
            await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=grant["code"], device_name="C")
        # A wrong code never costs the current membership.
        with pytest.raises(enrollment.EnrollmentError):
            await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code="AAAA-AAAA-AAAA", device_name="C",
                                              leave_current=True)
        assert engine.context() is not None and engine.context().network_id == solo
        await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=grant["code"], device_name="C",
                                          leave_current=True)
        await asyncio.wait_for(enrollment._task, timeout=10)
        ctx = engine.context()
        assert ctx is not None and ctx.network_id != solo and ctx.bootstrap_state == "ready"
        assert ctx.member(a.device_id).name == "A"
        assert database.get_system_setting("tts_model") == "voice-a"
        assert database.get_system_setting("only_on_c") == "yes"
        engine.scan_all()
    async with real_async(transport=served.transport()) as client:
        with c.active():
            results = await transport.sync_pass(client)
            assert results[a.device_id]["sent"] >= 1
    with a.active():
        assert database.get_system_setting("only_on_c") == "yes"
        assert database.get_system_setting("tts_model") == "voice-a"

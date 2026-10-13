"""Peer transport against a peer that answers badly: nothing unhandled, nothing unusable stored."""
import json
import time

import httpx
import pytest

from app.db import database
from app.db import network as store
from app.network import blobs, engine, peer_snapshot, transport
from tests.core.network.harness import join
from tests.core.network.test_transport import ServedBy

BASE = "https://a.example.com"


class Tampered(ServedBy):
    """``ServedBy`` whose answers for chosen paths are rewritten by ``edits[prefix](response)``."""

    def __init__(self, server, base_url):
        super().__init__(server, base_url)
        self.edits = {}

    def handler(self, request):
        response = super().handler(request)
        path = str(request.url)[len(self.base_url):].split("?")[0]
        for prefix, edit in self.edits.items():
            if path.startswith(prefix):
                return edit(response)
        return response


def _json_edit(change):
    def edit(response):
        body = json.loads(response.content)
        change(body)
        return httpx.Response(response.status_code, json=body)
    return edit


@pytest.fixture
def pair(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    return a, b, Tampered(a, BASE)


async def test_exchange_endpoints_and_capabilities_are_validated_before_storing(pair):
    a, b, served = pair
    served.edits["/api/v1/network/peer/exchange"] = _json_edit(lambda body: body.update(
        endpoints=["http://user:pw@a.lan:8000", "javascript:alert(1)", "https://a2.example.com/"],
        capabilities=["not", "a", "mapping"]))
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [BASE])
            await link.sync_once(client, engine.context())
            row = store.get_member(a.device_id)
            assert row["endpoints"] == ["https://a2.example.com"]
            assert not isinstance(row.get("capabilities"), list)
            assert link.endpoints == [BASE, "https://a2.example.com"]
            served.edits["/api/v1/network/peer/exchange"] = _json_edit(lambda body: body.update(endpoints=5))
            await link.sync_once(client, engine.context())
            assert store.get_member(a.device_id)["endpoints"] == ["https://a2.example.com"]


async def test_endpoint_answering_for_another_device_is_refused(pair):
    a, b, served = pair
    served.edits["/api/v1/network/peer/auth/token"] = _json_edit(lambda body: body.update(device_id="dev_else"))
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [BASE])
            with pytest.raises(transport.PeerError, match="another device"):
                await link.authenticate(client, engine.context())
            assert link.base_url is None and link.token is None
            with pytest.raises(transport.PeerError, match="no endpoint"):
                await link._request(client, "GET", "/api/v1/network/peer/ping")


@pytest.mark.parametrize("damage, message", [
    ("status", "interrupted"),
    ("extra", "truncated"),
    ("huge", "exceeds"),
])
async def test_state_transfer_refuses_damaged_pages(pair, monkeypatch, damage, message):
    a, b, served = pair

    def edit(response):
        if damage == "status":
            return httpx.Response(503, content=b"{}")
        return httpx.Response(200, content=response.content + b" ")

    served.edits["/api/v1/network/peer/state/"] = edit
    if damage == "huge":
        monkeypatch.setattr(peer_snapshot, "MAX_PAGE", 16)
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [BASE])
            with pytest.raises(transport.PeerError, match=message):
                await link.receive_state(client, engine.context())


async def test_sync_pass_backs_off_after_failures_and_reports_links(pair, monkeypatch):
    a, b, _served = pair
    with b.active():
        store.touch_member(a.device_id, endpoints=[BASE])

        async def unexpected(self, client, ctx, catch_up=False):
            raise RuntimeError("bug in a handler")

        monkeypatch.setattr(transport.PeerLink, "sync_once", unexpected)
        async with httpx.AsyncClient() as client:
            assert await transport.sync_pass(client) == {}
            link = transport._links[a.device_id]
            assert link.failures == 1 and link.backoff_until > time.monotonic()
            # Backing off: the next pass skips the peer.
            assert await transport.sync_pass(client) == {}
            assert link.failures == 1

            async def unreachable(self, client, ctx, catch_up=False):
                raise transport.PeerError("HTTP 502 unreachable")

            monkeypatch.setattr(transport.PeerLink, "sync_once", unreachable)
            link.backoff_until = 0
            await transport.sync_pass(client)
        assert link.failures == 2 and "502" in link.last_error
        status = transport.link_status()
        assert status[0]["device_id"] == a.device_id and status[0]["online"] is False
        assert status[0]["seconds_since_contact"] is None
        transport.note_contact(a.device_id)
        assert transport.link_status()[0]["online"] is True


async def test_malformed_file_part_fails_the_fetch_instead_of_raising(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = Tampered(a, BASE)
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_note(note_id="n1", title="Note", content="body")
        database.create_note_attachment(attachment_id="att1", note_id="n1", filename="x.txt", file_path="/tmp/x.txt",
                                        content_type="text/plain", size_bytes=5, data=b"hello")
        engine.scan_all()
    join(a, b)
    digest = blobs.sha256(b"hello")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=served.transport(), **kwargs))
    with b.active():
        assert b.sql("SELECT data FROM note_attachments WHERE id='att1'")[0]["data"] is None
        transport._links[a.device_id] = transport.PeerLink(a.device_id, [BASE])
        transport.note_contact(a.device_id)
        for bad in (b"[1, 2]", json.dumps({"signature": 7}).encode(), b"not json"):
            served.edits["/api/v1/network/peer/blob/"] = lambda response, bad=bad: httpx.Response(200, content=bad)
            assert await blobs.fetch_one(digest) is False
            assert await blobs.fetch_missing() == 0
        served.edits.clear()
        assert await blobs.fetch_one(digest) is True
        assert blobs.read_local(digest) == b"hello"
        assert await blobs.fetch_one(digest) is True  # already local

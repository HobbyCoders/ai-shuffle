"""The return-to-stable rule and the stable feed read (app/core/stable_return.py)."""
import base64
import json

import httpx
import pytest

from app.core import stable_return as sr

A, B = "a" * 40, "b" * 40


def envelope(payload):
    return json.dumps({"payload": base64.b64encode(json.dumps(payload).encode()).decode(), "signature": "c2ln"})


@pytest.mark.parametrize("installed, allowed", [
    ({"revision": B, "sequence": None, "version": None}, True),           # the stable revision itself
    ({"revision": A, "sequence": 9, "version": "9.9.9"}, True),           # earlier on main; main order wins
    ({"revision": A, "sequence": 10, "version": "1.0.0"}, False),         # same ordinal, other revision
    ({"revision": A, "sequence": 11, "version": None}, False),            # ahead of stable
    ({"revision": A, "sequence": None, "version": "1.0.1"}, True),        # a stable release before it
    ({"revision": A, "sequence": None, "version": "1.0.2"}, True),
    ({"revision": A, "sequence": None, "version": "1.0.3"}, False),
    ({"revision": A, "sequence": None, "version": "1.0.2-dev.4+gabc"}, False),  # a nightly: cannot tell
    ({"revision": None, "sequence": None, "version": None}, False),
])
def test_refusal(installed, allowed):
    stable = {"revision": B, "version": "1.0.2", "sequence": 10}
    assert (sr.refusal(installed, stable) is None) is allowed


def test_release_fields_reads_the_container_ordinal_and_edge_alias():
    fields = sr.release_fields(envelope({"revision": A, "channel": "edge", "version": "1.0.2", "container": {"sequence": 5}}))
    assert fields == {"revision": A, "channel": "nightly", "version": "1.0.2", "sequence": 5}
    assert sr.release_fields(envelope({"revision": A, "container": {"sequence": True}}))["sequence"] is None
    with pytest.raises(ValueError):
        sr.release_fields(envelope({"revision": "nope"}))
    with pytest.raises(ValueError):
        sr.release_fields("x" * (sr.MAX_MANIFEST + 1))


@pytest.fixture
def feed(monkeypatch):
    seen = {}
    answer = {"status": 200, "body": envelope({"revision": B, "channel": "stable", "version": "1.0.2", "container": {"sequence": 10}})}
    real = httpx.Client

    def handler(request):
        seen["url"], seen["headers"] = str(request.url), dict(request.headers)
        return httpx.Response(answer["status"], content=answer["body"].encode())
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen, answer


def test_fetch_stable_uses_public_hub_feed_without_a_lease(feed):
    seen, _ = feed
    text, stable = sr.fetch_stable()
    assert seen["url"] == "https://hobbycoders.com/v1/desktop/manifest"
    assert "x-shuffle-lease" not in seen["headers"]
    assert stable["revision"] == B and stable["sequence"] == 10 and json.loads(text)["payload"]


@pytest.mark.parametrize("status, body, message", [
    (402, "{}", r"did not answer \(402\)"),
    (200, "not json", "unreadable"),
    (200, envelope({"revision": B, "channel": "nightly"}), "did not return a stable release"),
])
def test_fetch_stable_failures_refuse(feed, status, body, message):
    _, answer = feed
    answer.update(status=status, body=body)
    with pytest.raises(sr.Refused, match=message):
        sr.fetch_stable()

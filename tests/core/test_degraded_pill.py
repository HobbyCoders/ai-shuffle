"""Tests for the V2 Q7 derive-on-read ``degraded`` session state.

Covers ``sessions_helpers.is_session_degraded`` — the shared derivation used by
the read-path enrichment. The single oracle is ``placement.resolve_placement``
(stubbed here so the matrix exercises the derivation logic itself, not the
resolver's internals). The board chip refreshes via the existing focus/
visibility board refetch (like ``device_online``); there is no sweep tick.

``degraded`` is transient/derive-on-read: nothing is persisted, and any
exception in the derivation must yield ``False`` (never a 500).
"""

from app.core import placement
from app.core.config import settings
from app.core.placement import PlacementDecision
from app.api.sessions_helpers import is_session_degraded


def _none(*_a, **_k):
    return PlacementDecision("none", None, "no capable runner online")


def _runner(*_a, **_k):
    return PlacementDecision("runner", "peer-1", "elected capable runner peer-1")


def _raise(*_a, **_k):
    raise RuntimeError("resolver blew up")


SID = "s-degraded"


def _session(sid=SID):
    # placement/instance_id are what resolve_placement would read; irrelevant
    # here because the resolver is stubbed, but present for realism.
    return {"id": sid, "placement": None, "instance_id": None}


# ---------------------------------------------------------------------------
# is_session_degraded — enrichment/derivation matrix
# ---------------------------------------------------------------------------

def test_queued_and_placement_none_is_degraded(monkeypatch):
    monkeypatch.setattr(settings, "v2_placement", True)
    monkeypatch.setattr(placement, "resolve_placement", _none)
    assert is_session_degraded(_session(), {SID}) is True


def test_queued_and_placement_runner_is_not_degraded(monkeypatch):
    monkeypatch.setattr(settings, "v2_placement", True)
    monkeypatch.setattr(placement, "resolve_placement", _runner)
    assert is_session_degraded(_session(), {SID}) is False


def test_not_queued_is_not_degraded_and_never_resolves(monkeypatch):
    """A session with no queued-no-run turn (absent from the candidate set) is
    never degraded, and the resolver is never consulted for it."""
    monkeypatch.setattr(settings, "v2_placement", True)
    called = {"n": 0}

    def _tracking(*_a, **_k):
        called["n"] += 1
        return _none()

    monkeypatch.setattr(placement, "resolve_placement", _tracking)
    # claimable set does NOT contain SID (even though placement would be "none").
    assert is_session_degraded(_session(), set()) is False
    assert called["n"] == 0


def test_flag_off_is_not_degraded_and_never_resolves(monkeypatch):
    """Flag OFF ⇒ False AND resolve_placement is NEVER called (raising stub
    proves the short-circuit)."""
    monkeypatch.setattr(settings, "v2_placement", False)
    monkeypatch.setattr(placement, "resolve_placement", _raise)
    # Even a fully-degraded-looking candidate stays False with the flag off.
    assert is_session_degraded(_session(), {SID}) is False


def test_resolver_exception_fails_open_to_false(monkeypatch):
    """A resolver exception ⇒ degraded False (the caller/listing never raises)."""
    monkeypatch.setattr(settings, "v2_placement", True)
    monkeypatch.setattr(placement, "resolve_placement", _raise)
    assert is_session_degraded(_session(), {SID}) is False

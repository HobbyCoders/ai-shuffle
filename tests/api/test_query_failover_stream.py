"""Frame semantics of ``app.api.query._stream_query_with_failover``.

Native account failover was retired (OC-1), so the wrapper is a single pass with
no retry and no invisible account switch:

* 429 ``api_error`` frames are held back and FLUSHED just before the terminal
  ``done`` frame; non-429 ``api_error`` frames pass through immediately.
* on a usage-limit ``done`` the OC-1/OC-4 rate-limited notification is surfaced
  via ``attempt_failover`` (which always returns ``retry=False`` — the call
  exists only for its notification side effect), and no ``account_switched``
  system frame is ever emitted.

``stream_query`` and ``attempt_failover`` are looked up as module attributes at
call time (``app.api.query.stream_query`` and, via a call-time
``from app.core.account_failover import ...``, ``account_failover.*``), so we
patch them where they're resolved.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import app.api.query as query_mod


def _stream_factory(*batches):
    """Return a ``stream_query`` stand-in yielding a fresh async-gen per call."""
    it = iter(batches)

    def _make(*args, **kwargs):
        frames = next(it)

        async def _gen():
            for f in frames:
                yield f

        return _gen()

    return _make


async def _collect(agen):
    return [ev async for ev in agen]


async def test_usage_limit_done_notifies_and_flushes_429_without_switch():
    only = [
        {"type": "init", "session_id": "s1"},
        {"type": "text", "content": "partial"},
        {"type": "api_error", "status": 429, "session_id": "s1"},
        {"type": "done", "session_id": "s1", "metadata": {"api_error_status": 429}},
    ]

    decision = MagicMock(retry=False, account_name=None)
    with patch.object(query_mod, "stream_query", new=_stream_factory(only)), \
         patch("app.core.account_failover.attempt_failover",
               new=AsyncMock(return_value=decision)) as af_mock:
        events = await _collect(query_mod._stream_query_with_failover(
            prompt="hi", profile_id="claude-code", session_id="s1",
        ))

    # The usage-limit done triggers the OC-4 notification side effect exactly once.
    af_mock.assert_awaited_once()
    types = [e.get("type") for e in events]
    # Single pass → exactly one init.
    assert types.count("init") == 1
    # Failover retired → no account-switch system frame is ever emitted.
    assert not any(e.get("type") == "system" and e.get("subtype") == "account_switched"
                   for e in events)
    # Held 429 is flushed just before the terminal usage-limit done.
    assert types == ["init", "text", "api_error", "done"]
    assert events[2]["status"] == 429
    assert events[-1]["metadata"]["api_error_status"] == 429
    # The streamed text passed through.
    assert events[1]["content"] == "partial"


async def test_give_up_flushes_held_429_before_done():
    only = [
        {"type": "init", "session_id": "s1"},
        {"type": "api_error", "status": 429, "session_id": "s1"},
        {"type": "done", "session_id": "s1", "metadata": {"api_error_status": 429}},
    ]

    decision = MagicMock(retry=False, account_name=None)
    with patch.object(query_mod, "stream_query", new=_stream_factory(only)), \
         patch("app.core.account_failover.attempt_failover",
               new=AsyncMock(return_value=decision)):
        events = await _collect(query_mod._stream_query_with_failover(
            prompt="hi", profile_id="claude-code", session_id="s1",
        ))

    types = [e.get("type") for e in events]
    # On give-up the held api_error IS surfaced, and it precedes the done frame.
    assert types == ["init", "api_error", "done"]
    assert events[1]["status"] == 429
    assert events[-1]["metadata"]["api_error_status"] == 429


async def test_non_429_api_error_passes_through_immediately():
    only = [
        {"type": "init", "session_id": "s1"},
        {"type": "api_error", "status": 500, "session_id": "s1"},
        {"type": "done", "session_id": "s1", "metadata": {}},
    ]

    with patch.object(query_mod, "stream_query", new=_stream_factory(only)), \
         patch("app.core.account_failover.attempt_failover",
               new=AsyncMock()) as af_mock:
        events = await _collect(query_mod._stream_query_with_failover(
            prompt="hi", profile_id="claude-code", session_id="s1",
        ))

    # A clean (non-usage-limit) done never triggers failover; the 500 error is
    # forwarded verbatim (not held).
    af_mock.assert_not_awaited()
    assert [e.get("type") for e in events] == ["init", "api_error", "done"]
    assert events[1]["status"] == 500

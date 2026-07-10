"""Frame semantics of ``app.api.query._stream_query_with_failover``.

The transparent-retry wrapper must make an auto-switch invisible to the SSE
consumer:

* the retry's duplicate ``init`` frame is consumed (only the first stream's
  ``init`` is forwarded), and
* the failed attempt's 429 ``api_error`` frame is held back — DROPPED when the
  failover retries (superseded), FLUSHED just before the terminal ``done`` when
  the failover gives up.

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


async def test_retry_hides_init_and_drops_429_api_error():
    first = [
        {"type": "init", "session_id": "s1"},
        {"type": "text", "content": "partial"},
        {"type": "api_error", "status": 429, "session_id": "s1"},
        {"type": "done", "session_id": "s1", "metadata": {"api_error_status": 429}},
    ]
    second = [
        {"type": "init", "session_id": "s1"},
        {"type": "text", "content": "final"},
        {"type": "done", "session_id": "s1", "metadata": {}},
    ]

    decision = MagicMock(retry=True, account_name="B")
    with patch.object(query_mod, "stream_query", new=_stream_factory(first, second)), \
         patch("app.core.account_failover.attempt_failover",
               new=AsyncMock(return_value=decision)):
        events = await _collect(query_mod._stream_query_with_failover(
            prompt="hi", profile_id="claude-code", session_id="s1",
        ))

    types = [e.get("type") for e in events]
    # Exactly ONE init forwarded (the retry's is consumed).
    assert types.count("init") == 1
    # No 429 api_error leaked.
    assert not any(e.get("type") == "api_error" and e.get("status") == 429
                   for e in events)
    # The account-switch system frame was surfaced.
    assert any(e.get("type") == "system" and e.get("subtype") == "account_switched"
               for e in events)
    # Terminal frame is the clean (non-429) done.
    assert events[-1]["type"] == "done"
    assert not events[-1]["metadata"]
    # Both attempts' text passed through.
    assert {"partial", "final"} <= {e.get("content") for e in events if e.get("type") == "text"}


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

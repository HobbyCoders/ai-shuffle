"""Client-disconnect guards on the mobile/query SSE generators (issue #634).

Follow-up to #627 / PR #633, which added the disconnect classifier + except
guard to ``app.api.events.event_stream``. The same unguarded shape lived in:

* ``app.api.mobile.notifications_stream`` -> ``event_generator``
* ``app.api.query.stream_one_shot_query`` -> ``event_generator``
* ``app.api.query.stream_conversation`` -> ``event_generator``

On disconnect Starlette cancels the streaming task, throwing the exception into
the generator at its current ``await``. If it escapes, the app's
``BaseHTTPMiddleware`` stack collapses it into an anyio ``ExceptionGroup`` that
surfaces as an unhandled 500 + error_log row instead of a clean close.

These generators are closures built inside their route functions and their SSE
bodies never end, so — per the repo SSE-test constraint — we NEVER drive them
through a buffering ASGI test client. Instead we call the route function
directly, pull the generator off the returned ``StreamingResponse.body_iterator``,
and inject the exact exception shapes at the live suspension point with
``agen.athrow(...)``.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from starlette.requests import ClientDisconnect

import app.api.mobile as mobile_mod
import app.api.query as query_mod
from app.services.mobile_push_hub import mobile_push_hub


def _inner_factory(*frames):
    """Return a ``_stream_query_with_failover`` stand-in yielding ``frames``."""

    def _make(*args, **kwargs):
        async def _gen():
            for f in frames:
                yield f

        return _gen()

    return _make


# ---------------------------------------------------------------------------
# mobile.py :: notifications_stream -> event_generator
# ---------------------------------------------------------------------------


async def _mobile_agen():
    """Start the mobile SSE generator, parked at its first yield.

    Auth is stubbed; the real (in-process) push hub is used so the finally-clause
    unsubscribe is observable.
    """
    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)
    with patch.object(mobile_mod, "get_device_from_api_key", return_value={"device_id": "d1"}):
        response = await mobile_mod.notifications_stream(request)
    agen = response.body_iterator
    first = await agen.__anext__()  # ": connected\n\n" -> parked at that yield
    assert first == ": connected\n\n"
    return agen


async def test_mobile_cancellederror_ends_cleanly():
    before = mobile_push_hub.subscriber_count
    agen = await _mobile_agen()
    assert mobile_push_hub.subscriber_count == before + 1

    with pytest.raises(StopAsyncIteration):
        await agen.athrow(asyncio.CancelledError())

    # finally still unsubscribed; nothing escaped.
    assert mobile_push_hub.subscriber_count == before


async def test_mobile_clientdisconnect_ends_cleanly():
    before = mobile_push_hub.subscriber_count
    agen = await _mobile_agen()

    with pytest.raises(StopAsyncIteration):
        await agen.athrow(ClientDisconnect())

    assert mobile_push_hub.subscriber_count == before


async def test_mobile_exceptiongroup_disconnect_ends_cleanly():
    before = mobile_push_hub.subscriber_count
    agen = await _mobile_agen()

    grp = BaseExceptionGroup("disconnect", [asyncio.CancelledError()])
    with pytest.raises(StopAsyncIteration):
        await agen.athrow(grp)

    assert mobile_push_hub.subscriber_count == before


async def test_mobile_real_error_propagates():
    before = mobile_push_hub.subscriber_count
    agen = await _mobile_agen()

    with pytest.raises(RuntimeError, match="boom"):
        await agen.athrow(RuntimeError("boom"))

    # unsubscribed even on the error path.
    assert mobile_push_hub.subscriber_count == before


async def test_mobile_exceptiongroup_real_error_propagates():
    before = mobile_push_hub.subscriber_count
    agen = await _mobile_agen()

    grp = BaseExceptionGroup("mixed", [asyncio.CancelledError(), RuntimeError("boom")])
    with pytest.raises(BaseExceptionGroup):
        await agen.athrow(grp)

    assert mobile_push_hub.subscriber_count == before


# ---------------------------------------------------------------------------
# query.py :: stream_one_shot_query / stream_conversation -> event_generator
#
# Both wrap ``_stream_query_with_failover``; both already carry an
# ``except Exception`` clause that turns genuine errors into an ``event: error``
# frame. The new disconnect clause sits BEFORE it, so pure disconnects end
# cleanly, group-wrapped real errors propagate, and ordinary Exceptions keep
# their existing error-frame behavior.
# ---------------------------------------------------------------------------


async def _query_agen(route):
    """Start a query SSE generator, parked at its first yielded frame."""
    request = MagicMock()
    request.prompt = "hi"
    request.overrides = None
    api_user = {"id": "u1", "profile_id": "p1", "project_id": "proj1"}
    with patch.object(
        query_mod,
        "_stream_query_with_failover",
        new=_inner_factory({"type": "text", "content": "partial"}),
    ):
        response = await route(request=request, api_user=api_user, _=None)
        agen = response.body_iterator
        first = await agen.__anext__()  # first data frame -> parked at that yield
    assert first.startswith("event: text")
    return agen


@pytest.mark.parametrize(
    "route",
    [query_mod.stream_one_shot_query, query_mod.stream_conversation],
)
async def test_query_cancellederror_ends_cleanly(route):
    agen = await _query_agen(route)
    with pytest.raises(StopAsyncIteration):
        await agen.athrow(asyncio.CancelledError())


@pytest.mark.parametrize(
    "route",
    [query_mod.stream_one_shot_query, query_mod.stream_conversation],
)
async def test_query_exceptiongroup_disconnect_ends_cleanly(route):
    agen = await _query_agen(route)
    grp = BaseExceptionGroup("disconnect", [asyncio.CancelledError()])
    with pytest.raises(StopAsyncIteration):
        await agen.athrow(grp)


@pytest.mark.parametrize(
    "route",
    [query_mod.stream_one_shot_query, query_mod.stream_conversation],
)
async def test_query_exceptiongroup_real_error_propagates(route):
    agen = await _query_agen(route)
    grp = BaseExceptionGroup("mixed", [asyncio.CancelledError(), RuntimeError("boom")])
    with pytest.raises(BaseExceptionGroup):
        await agen.athrow(grp)


@pytest.mark.parametrize(
    "route",
    [query_mod.stream_one_shot_query, query_mod.stream_conversation],
)
async def test_query_ordinary_error_still_yields_error_frame(route):
    """A plain non-disconnect Exception keeps the pre-existing behavior: it is
    caught by the generator's ``except Exception`` and surfaced as an
    ``event: error`` frame, NOT re-raised."""
    agen = await _query_agen(route)
    frame = await agen.athrow(RuntimeError("boom"))
    assert frame.startswith("event: error")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["type"] == "error"
    assert "boom" in payload["message"]

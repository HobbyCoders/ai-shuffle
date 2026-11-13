"""Only explicitly passive subscriptions close at the HTTP drain signal."""
import asyncio

import pytest
from starlette.responses import StreamingResponse

from app.api.sse_utils import PassiveEventResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("event_already_set", [False, True])
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_passive_stream_closes_cleanly_and_runs_finalizer(event_already_set, spec_version):
    stopping = asyncio.Event()
    started = asyncio.Event()
    finalized = asyncio.Event()
    messages = []

    async def events():
        try:
            started.set()
            yield b": connected\n\n"
            await asyncio.Event().wait()
        finally:
            finalized.set()

    async def send(message):
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    if event_already_set:
        stopping.set()
    response = PassiveEventResponse(events(), media_type="text/event-stream")
    task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": spec_version},
                                        "shuffle.http_shutdown": stopping}, receive, send))
    if not event_already_set:
        await asyncio.wait_for(started.wait(), 1)
        stopping.set()
    await asyncio.wait_for(task, 2)
    assert finalized.is_set()
    assert messages[0]["type"] == "http.response.start"
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert sum(message["type"] == "http.response.body" and not message.get("more_body", False)
               for message in messages) == 1


@pytest.mark.asyncio
async def test_passive_peer_cannot_block_the_final_send():
    stopping = asyncio.Event()
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def events():
        try:
            started.set()
            yield b": connected\n\n"
            await asyncio.Event().wait()
        finally:
            finalized.set()

    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body"):
            await asyncio.Event().wait()

    async def receive():
        await asyncio.Event().wait()

    response = PassiveEventResponse(events(), media_type="text/event-stream")
    task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"},
                                        "shuffle.http_shutdown": stopping}, receive, send))
    await asyncio.wait_for(started.wait(), 1)
    stopping.set()
    await asyncio.wait_for(task, 2)
    assert finalized.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("passive", [False, True])
async def test_finite_response_completes_normally(passive):
    stopping = asyncio.Event()
    messages = []

    async def body():
        yield b"one"
        yield b"two"

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    response_type = PassiveEventResponse if passive else StreamingResponse
    response = response_type(body(), media_type="text/event-stream")
    await response({"type": "http", "asgi": {"spec_version": "2.4"},
                    "shuffle.http_shutdown": stopping}, receive, send)
    assert b"".join(message.get("body", b"") for message in messages) == b"onetwo"
    assert sum(message["type"] == "http.response.body" and not message.get("more_body", False)
               for message in messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event_already_set", [False, True])
async def test_drain_through_base_http_middleware_ends_cleanly(event_already_set):
    """#1532: a stream opened or reconnecting during the drain must still send
    its response start, or BaseHTTPMiddleware fails its start assertion."""
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Route

    class PassThrough(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

    started = asyncio.Event()

    async def events():
        started.set()
        yield b": connected\n\n"
        await asyncio.Event().wait()

    async def endpoint(request):
        return PassiveEventResponse(events(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/events", endpoint)])
    app.add_middleware(PassThrough)
    stopping = asyncio.Event()
    if event_already_set:
        stopping.set()
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "GET", "path": "/events", "raw_path": b"/events",
             "query_string": b"", "headers": [], "scheme": "http", "server": ("test", 80),
             "client": ("peer", 1), "root_path": "", "shuffle.http_shutdown": stopping}
    task = asyncio.create_task(app(scope, receive, send))
    if not event_already_set:
        await asyncio.wait_for(started.wait(), 1)
        stopping.set()
    await asyncio.wait_for(task, 2)
    assert messages[0]["type"] == "http.response.start"
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert sum(message["type"] == "http.response.start" for message in messages) == 1

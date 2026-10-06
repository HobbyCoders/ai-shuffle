"""Session-scoped browser transport between authenticated network members."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatusCode,
    WebSocketException,
)
from websockets.legacy.client import connect

from app.db import network as store
from app.db.async_database import run_sync
from app.network import control, engine, tokens, transport

MAX_MESSAGE = 10 * 1024 * 1024
AUTH_CHECK_SECONDS = 5
UNAVAILABLE = "The session's worker is unavailable. Reconnect when it is online."


class PeerConnect(connect):
    def handle_redirect(self, uri):
        # Never send a device credential to a redirected host.
        raise InvalidHandshake("Peer browser redirects are not allowed")


async def peer_link(device_id: str):
    ctx = engine.require_context()
    member = ctx.member(device_id)
    if member is None or member.status != "active":
        raise HTTPException(503, UNAVAILABLE)
    link = transport._links.get(device_id)
    if link is None:
        from app.network.endpoints import peer_endpoints
        row = await run_sync(store.get_member, device_id)
        link = transport.PeerLink(device_id, peer_endpoints(row, engine.presence_of(device_id)))
        transport._links[device_id] = link
    if not link.endpoints:
        raise HTTPException(503, UNAVAILABLE)
    return ctx, link


def peer_path(session_id: str) -> str:
    return f"/api/v1/network/peer/browser/{quote(session_id, safe='')}"


async def forward_http(
    request: Request, device_id: str, session_id: str, operation: str
) -> Response:
    from app.core.browser_uploads import MAX_BODY

    path = peer_path(session_id) + ("/" + operation if operation else "")
    return await forward_buffered(request, device_id, path, max_body=MAX_BODY)


async def forward_buffered(
    request: Request,
    device_id: str,
    path: str,
    *,
    max_body: int,
    noun: str = "browser",
    timeout: float = 60,
) -> Response:
    """Relay one buffered HTTP request to ``path`` on a member worker.

    The peer's status and body pass through unchanged, so its own refusals
    (a path outside its allow-list, an oversized upload) reach the caller as
    it phrased them. A 401 is retried once with a fresh device token; any other
    response is returned as-is, so a mutation is never replayed.
    """
    ctx, link = await peer_link(device_id)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_body:
            raise HTTPException(413, f"{noun.capitalize()} request too large")
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            for attempt in range(2):
                await link.authenticate(client, ctx)
                headers = {"Authorization": "Bearer " + link.token}
                if request.headers.get("content-type"):
                    headers["Content-Type"] = request.headers["content-type"]
                async with client.stream(
                    request.method,
                    link.base_url + path,
                    headers=headers,
                    params=request.query_params.multi_items(),
                    content=bytes(body),
                    timeout=timeout,
                ) as response:
                    if response.status_code == 401 and attempt == 0:
                        link.token = None
                        continue  # Rejected before dispatch; mutations are not replayed otherwise.
                    if 300 <= response.status_code < 400:
                        raise HTTPException(
                            502, f"The session's worker redirected the {noun} request"
                        )
                    result = bytearray()
                    async for chunk in response.aiter_bytes():
                        result.extend(chunk)
                        if len(result) > MAX_MESSAGE:
                            raise HTTPException(502, f"{noun.capitalize()} response too large")
                    transport.note_contact(device_id)
                    return Response(
                        bytes(result),
                        status_code=response.status_code,
                        media_type=response.headers.get(
                            "content-type", "application/json"
                        ),
                    )
    except (httpx.HTTPError, transport.PeerError):
        raise HTTPException(503, UNAVAILABLE) from None


async def _until_not_member(device_id: str) -> None:
    """Return once ``device_id`` stops being an active member (checked every few seconds).

    The peer side re-checks the viewer's device token as often; this is the
    viewer side's half, so removing the owning device ends the relay.
    """
    while True:
        await asyncio.sleep(AUTH_CHECK_SECONDS)
        ctx = engine.context()
        member = ctx.member(device_id) if ctx is not None else None
        if member is None or member.status != "active":
            return


async def relay(websocket: WebSocket, upstream, device_id: str | None = None) -> None:
    async def to_peer():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            payload = (
                message.get("text")
                if message.get("text") is not None
                else message.get("bytes")
            )
            if payload is not None:
                if len(payload) > MAX_MESSAGE:
                    await websocket.close(code=1009)
                    return
                await upstream.send(payload)

    async def to_viewer():
        async for payload in upstream:
            if isinstance(payload, bytes):
                await websocket.send_bytes(payload)
            else:
                await websocket.send_text(payload)

    tasks = [asyncio.create_task(to_peer()), asyncio.create_task(to_viewer())]
    if device_id is not None:
        tasks.append(asyncio.create_task(_until_not_member(device_id)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def forward_websocket(
    websocket: WebSocket, device_id: str, session_id: str
) -> None:
    try:
        ctx, link = await peer_link(device_id)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            for attempt in range(2):
                await link.authenticate(client, ctx)
                url = urlsplit(link.base_url)
                uri = urlunsplit(
                    (
                        "wss" if url.scheme == "https" else "ws",
                        url.netloc,
                        url.path.rstrip("/") + peer_path(session_id),
                        "",
                        "",
                    )
                )
                try:
                    async with PeerConnect(
                        uri,
                        extra_headers={"Authorization": "Bearer " + link.token},
                        open_timeout=15,
                        close_timeout=3,
                        max_size=MAX_MESSAGE,
                        max_queue=2,
                    ) as upstream:
                        transport.note_contact(device_id)
                        await relay(websocket, upstream, device_id)
                    break
                except InvalidStatusCode as exc:
                    if exc.status_code == 401 and attempt == 0:
                        link.token = None
                        continue
                    raise
    except ConnectionClosed as exc:
        # Token renewal and ownership changes deliberately reconnect. They are
        # not outages and must not leave an error banner over a recovered feed.
        if exc.rcvd is None or exc.rcvd.code not in (1000, 1001, 1013):
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.send_json({"type": "error", "message": UNAVAILABLE})
    except (
        HTTPException,
        httpx.HTTPError,
        transport.PeerError,
        OSError,
        TimeoutError,
        WebSocketException,
    ):
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json({"type": "error", "message": UNAVAILABLE})
    except WebSocketDisconnect:
        return
    finally:
        # 1013 permits the normal panel reconnect; never attach a local fallback.
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=1013, reason="Reconnect to the session's worker")


async def require_owned(session_id: str) -> None:
    try:
        await run_sync(control._require_owned, session_id)
    except LookupError:
        raise HTTPException(404, "Session not found") from None
    except PermissionError:
        raise HTTPException(409, "This worker does not own the session") from None


async def serve_peer(websocket: WebSocket, session_id: str) -> None:
    from app.api.browser_panel import serve_browser_panel

    # Peer credentials are accepted only here, never on the public browser API.
    try:
        await run_sync(tokens.require_worker_device, websocket)
        await require_owned(session_id)
    except HTTPException as exc:
        await websocket.send_denial_response(Response(status_code=exc.status_code))
        return
    await websocket.accept()

    async def watch_authority():
        while True:
            await asyncio.sleep(AUTH_CHECK_SECONDS)
            await run_sync(tokens.require_worker_device, websocket)
            await require_owned(session_id)

    tasks = [
        asyncio.create_task(serve_browser_panel(websocket, session_id)),
        asyncio.create_task(watch_authority()),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except HTTPException:
        await websocket.close(code=1013, reason="Browser authority changed; reconnect")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

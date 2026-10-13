"""Streaming downloads from the worker that owns a chat session."""
from urllib.parse import quote

import anyio
import httpx
from fastapi import HTTPException, Request
from starlette.responses import StreamingResponse

from app.network import transport
from app.network.browser_panel import UNAVAILABLE, peer_link


async def forward_file(request: Request, owner: str, session_id: str, path: str, project_id: str | None):
    params = {"path": path}
    if project_id:
        params["project_id"] = project_id
    return await forward_download(
        request, owner, "/api/files/peer/" + quote(session_id, safe=""), params,
    )


async def forward_download(request: Request, owner: str, endpoint: str, params: dict):
    """Proxy a server-selected download endpoint, preserving byte ranges."""
    ctx, link = await peer_link(owner)
    client = httpx.AsyncClient(follow_redirects=False)
    response = None
    try:
        for attempt in range(2):
            await link.authenticate(client, ctx)
            headers = {"Authorization": "Bearer " + link.token, "Accept-Encoding": "identity"}
            for name in ("range", "if-range"):
                if name in request.headers:
                    headers[name] = request.headers[name]
            upstream = client.build_request(
                "GET", link.base_url + endpoint,
                params=params, headers=headers, timeout=60,
            )
            response = await client.send(upstream, stream=True)
            if response.status_code == 401 and attempt == 0:
                await response.aclose()
                link.token = None
                continue
            break
        if 300 <= response.status_code < 400:
            raise HTTPException(502, "The session's worker redirected the file request")
        transport.note_contact(owner)
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            if response is not None:
                await response.aclose()
            await client.aclose()
        if isinstance(exc, (httpx.HTTPError, transport.PeerError)):
            raise HTTPException(503, UNAVAILABLE) from None
        raise

    async def chunks():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            # StreamingResponse cancels its task group when the viewer disconnects.
            with anyio.CancelScope(shield=True):
                await response.aclose()
                await client.aclose()

    headers = {name: response.headers[name] for name in (
        "content-type", "content-disposition", "content-length", "content-range",
        "accept-ranges", "etag", "last-modified", "content-encoding",
    ) if name in response.headers}
    return StreamingResponse(chunks(), status_code=response.status_code, headers=headers)

"""An httpx transport that rewrites upstream vendor URLs onto the stub.

TEST / DEV TOOLING ONLY — never imported from ``app/``.

Every gateway network entry point takes an optional ``client:
httpx.AsyncClient``. Handing it a client built here means a test exercises the
REAL ``upstream.py`` code (headers, status handling, ``iter_sse_events``) over a
REAL TCP socket to the stub — no mock transport, no monkeypatched constants, no
network egress.

Prefer this for tests. For a running app instance (which has no client seam)
use the MITM proxy in ``proxy.py`` instead.
"""

from __future__ import annotations

from typing import Iterable, Optional

import httpx

# The hosts app/core/translator_gateway/upstream.py talks to.
VENDOR_HOSTS = ("chatgpt.com", "api.openai.com", "api.anthropic.com")


class StubRedirectTransport(httpx.AsyncBaseTransport):
    """Rewrite ``https://<vendor-host>/...`` to ``http://127.0.0.1:<port>/...``.

    The path, method, headers and body are passed through untouched, so what the
    stub records is byte-for-byte what the gateway built.
    """

    def __init__(
        self,
        port: int,
        *,
        host: str = "127.0.0.1",
        vendor_hosts: Iterable[str] = VENDOR_HOSTS,
        inner: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._port = int(port)
        self._host = host
        self._hosts = {h.lower() for h in vendor_hosts}
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        if original_host.lower() in self._hosts:
            # NOTE: the ``Host`` header was already built from the vendor URL and
            # is deliberately left alone, so the stub's request log shows the
            # host the gateway believed it was calling.
            request.url = request.url.copy_with(
                scheme="http", host=self._host, port=self._port
            )
            request.headers["x-stub-original-host"] = original_host
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def stub_client(port: int, **kwargs) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose vendor traffic lands on the stub."""
    kwargs.setdefault("timeout", httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0))
    return httpx.AsyncClient(transport=StubRedirectTransport(port), **kwargs)

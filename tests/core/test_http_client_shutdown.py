"""The shared HTTPX client must actually release its async transport at shutdown."""
import httpx
import pytest

from app.core import http_client


@pytest.mark.asyncio
async def test_shutdown_closes_real_httpx_client_and_allows_reinitialization(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    monkeypatch.setattr(http_client, '_client', client)
    await http_client.close_http_client()
    assert client.is_closed
    assert http_client._client is None
    await http_client.close_http_client()  # repeated shutdown is harmless
    replacement = http_client.get_http_client()
    assert replacement is not client and not replacement.is_closed
    await http_client.close_http_client()
    assert replacement.is_closed

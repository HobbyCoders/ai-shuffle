"""Shared HTTP client for outbound requests."""
import httpx

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=True,
        )
    return _client


async def close_http_client() -> None:
    """Close the shared HTTP client. Call on app shutdown."""
    global _client
    client, _client = _client, None
    if client is not None and not client.is_closed:
        await client.aclose()

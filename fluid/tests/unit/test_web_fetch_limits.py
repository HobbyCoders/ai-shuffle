"""WebFetch/WebSearch edges: the body cap bounds the read, odd charsets and
arguments stay in-context, redirects are re-vetted, search failures report.

Hermetic: DNS is replaced by a fake resolver and HTTP by httpx.MockTransport.
"""

from __future__ import annotations

import httpx
import pytest

from fluid.tools.builtins import web
from fluid.tools.builtins.web import (
    PinnedTransport,
    _blocked_address,
    _detail_links,
    _extract,
    check_url,
    fetch_handler,
    navigation_note,
    resolve_public,
    search_handler,
)
from fluid.tools.registry import ToolCtx


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    """Every name resolves to one public address unless a test overrides it."""

    def getaddrinfo(host, *_a, **_k):
        if host.endswith(".internal"):
            return [(0, 0, 0, "", ("10.1.2.3", 0))]
        return [(0, 0, 0, "", ("93.184.215.14", 0))]

    monkeypatch.setattr(web.socket, "getaddrinfo", getaddrinfo)


def _ctx(handler) -> ToolCtx:
    return ToolCtx(extra={"web_transport": httpx.MockTransport(handler)})


# ─── resolution / URL checks ─────────────────────────────────────────────────


def test_resolve_public_unresolvable_and_unparseable(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise OSError("nxdomain")

    monkeypatch.setattr(web.socket, "getaddrinfo", boom)
    assert resolve_public("nope.example") == (None, "cannot resolve host 'nope.example'")

    monkeypatch.setattr(
        web.socket, "getaddrinfo", lambda *_a, **_k: [(0, 0, 0, "", ("not-an-ip", 0))]
    )
    address, reason = resolve_public("weird.example")
    assert address is None and "cannot resolve" in reason


def test_resolve_public_strips_ipv6_zone_and_refuses_any_private_answer(monkeypatch) -> None:
    monkeypatch.setattr(web.socket, "getaddrinfo", lambda *_a, **_k: [
        (0, 0, 0, "", ("2606:2800:220:1::1%eth0", 0, 0, 0)),
        (0, 0, 0, "", ("127.0.0.1", 0)),
    ])
    address, reason = resolve_public("mixed.example")
    assert address is None and "private/loopback" in reason


def test_blocked_address_covers_mapped_and_multicast() -> None:
    import ipaddress

    assert _blocked_address(ipaddress.ip_address("::ffff:127.0.0.1"))
    assert _blocked_address(ipaddress.ip_address("ff02::1"))
    assert not _blocked_address(ipaddress.ip_address("8.8.8.8"))


def test_check_url_without_host() -> None:
    assert check_url("https:///path") == "URL has no host"


def test_proxy_detection(monkeypatch) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    assert web._proxy_configured() is False
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    assert web._proxy_configured() is True


async def test_pinned_transport_passes_through_when_host_is_the_address() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    transport = PinnedTransport(
        inner=httpx.MockTransport(respond),
        resolver=lambda host: (host, None),
    )
    async with transport:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("http://93.184.215.14/x")
    assert response.text == "ok"
    assert "sni_hostname" not in seen[0].extensions  # plain http: no SNI
    await transport.aclose()


# ─── WebFetch ────────────────────────────────────────────────────────────────


async def test_webfetch_refuses_a_private_target_before_any_request() -> None:
    def respond(request):  # pragma: no cover - must not run
        raise AssertionError("no request may be sent")

    outcome = await fetch_handler({"url": "https://db.internal/"}, _ctx(respond))
    assert outcome.is_error and "WebFetch refused" in outcome.text


async def test_webfetch_body_read_is_capped(monkeypatch) -> None:
    """The cap bounds what is READ: a response larger than the cap is cut
    after the cap, and the result says the body was cut."""
    monkeypatch.setattr(web, "_MAX_RESPONSE_BYTES", 1000)
    produced = {"bytes": 0}

    async def endless():
        for _ in range(10_000):  # 10 MB if fully drained
            produced["bytes"] += 1000
            yield b"a" * 1000

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=endless())

    outcome = await fetch_handler(
        {"url": "https://example.com/huge", "max_chars": 50_000}, _ctx(respond)
    )
    assert not outcome.is_error
    assert "only its beginning was read" in outcome.text
    assert outcome.text.count("a") >= 1000
    assert produced["bytes"] <= 3000  # stopped streaming right after the cap


async def test_webfetch_unknown_charset_falls_back_to_utf8() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain; charset=x-no-such-codec"},
            content="héllo".encode(),
        )

    outcome = await fetch_handler({"url": "https://example.com/t"}, _ctx(respond))
    assert not outcome.is_error
    assert "héllo" in outcome.text


async def test_webfetch_negative_max_chars_still_pages_forward() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"abcdef")

    outcome = await fetch_handler(
        {"url": "https://example.com/t", "max_chars": -5}, _ctx(respond)
    )
    # A window of at least one character: the continuation offset advances.
    assert "continue with offset=1" in outcome.text


async def test_webfetch_follows_a_same_host_redirect() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"moved here")

    outcome = await fetch_handler({"url": "https://example.com/old"}, _ctx(respond))
    assert not outcome.is_error and "moved here" in outcome.text


async def test_webfetch_same_host_redirect_to_a_bad_scheme_is_blocked() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "ftp://example.com/file"})

    outcome = await fetch_handler({"url": "https://example.com/a"}, _ctx(respond))
    assert outcome.is_error and "only http(s)" in outcome.text


async def test_webfetch_redirect_loop_stops() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    outcome = await fetch_handler({"url": "https://example.com/a"}, _ctx(respond))
    assert outcome.is_error and "too many redirects" in outcome.text


async def test_webfetch_transport_errors_stay_in_context() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    outcome = await fetch_handler({"url": "https://example.com/"}, _ctx(refused))
    assert outcome.is_error and "ConnectError" in outcome.text


async def test_webfetch_blocked_address_at_connect_time(monkeypatch) -> None:
    def respond(request):  # pragma: no cover - must not run
        raise AssertionError("no request may be sent")

    ctx = ToolCtx(extra={"web_transport": PinnedTransport(
        inner=httpx.MockTransport(respond),
        resolver=lambda host: (None, "rebound to 10.0.0.1"),
    )})
    outcome = await fetch_handler({"url": "https://example.com/"}, ctx)
    assert outcome.is_error and outcome.text == "WebFetch refused: rebound to 10.0.0.1"


async def test_webfetch_sniffs_html_without_a_content_type() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html><head><title>Sniffed</title></head><body>x</body></html>"
        )

    outcome = await fetch_handler({"url": "https://example.com/"}, _ctx(respond))
    assert outcome.text.startswith("https://example.com/ — Sniffed")


# ─── navigation diagnostic ───────────────────────────────────────────────────


def test_navigation_note_lists_detail_links_and_skips_chrome() -> None:
    links = "".join(
        f'<li><a href="/guide/topic-{i}">Guide topic number {i}</a></li>' for i in range(12)
    )
    chrome = (
        '<a href="/">Home</a><a href="https://twitter.com/x">Follow us</a>'
        '<a href="/docs/">docs</a><a href="/guide/topic-0">Guide topic number 0</a>'
        f'<a href="/deep">{"L" * 80}</a>'
    )
    html = f"<html><body><ul>{links}</ul>{chrome}</body></html>"
    extractor = _extract(html, "https://example.com/docs/")
    text = extractor.text()
    note = navigation_note(extractor, text, "https://example.com/docs/")
    assert "mostly navigation" in note
    assert "Guide topic number 1 — https://example.com/guide/topic-1" in note
    assert "twitter.com" not in note and "— https://example.com/ \n" not in note
    assert "(…and" in note  # more than eight candidates


def test_detail_links_truncates_long_anchor_and_drops_root() -> None:
    out = _detail_links(
        [("x" * 80, "https://example.com/a"), ("Site root link", "https://example.com/")],
        "https://example.com/index",
    )
    assert out == [("x" * 60 + "…", "https://example.com/a")]


def test_navigation_note_prose_page_has_none() -> None:
    extractor = _extract("<p>" + "prose " * 200 + "</p>", "https://example.com/")
    assert navigation_note(extractor, extractor.text(), "https://example.com/") == ""
    assert navigation_note(extractor, "", "https://example.com/") == ""


# ─── WebSearch ───────────────────────────────────────────────────────────────


async def test_websearch_empty_query_and_failures() -> None:
    def ok(request):  # pragma: no cover - not reached for an empty query
        return httpx.Response(200, text="")

    assert (await search_handler({"query": "  "}, _ctx(ok))).is_error

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    outcome = await search_handler({"query": "x"}, _ctx(boom))
    assert outcome.is_error and "ReadTimeout" in outcome.text

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    outcome = await search_handler({"query": "x"}, _ctx(rate_limited))
    assert outcome.is_error and "HTTP 429" in outcome.text


async def test_websearch_no_results_and_limit() -> None:
    page = "".join(
        f'<a class="result__a" href="https://site{i}.example/">R{i}</a>' for i in range(5)
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    outcome = await search_handler({"query": "x", "limit": 2}, _ctx(respond))
    assert outcome.text.startswith("2 result(s)")
    outcome = await search_handler(
        {"query": "x", "allowed_domains": ["*.nowhere.example"]}, _ctx(respond)
    )
    assert "no results for 'x'" in outcome.text

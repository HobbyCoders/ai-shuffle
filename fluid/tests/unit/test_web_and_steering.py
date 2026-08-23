"""Phase-4 features: web tools, mid-turn steering, availability probes."""

from __future__ import annotations

import httpx
import pytest

from fluid.context.steering import STEER_CLOSE, STEER_OPEN, format_steer
from fluid.events.ops import Steer
from fluid.tools.builtins.web import (
    check_url,
    fetch_handler,
    html_to_text,
    parse_search_results,
    search_handler,
)
from fluid.tools.hardening import UNTRUSTED_TAG
from fluid.tools.registry import (
    PROBE_GRACE_S,
    PROBE_TTL_S,
    ToolCtx,
    ToolRegistry,
    ToolSpec,
)


# ─── URL guarding ────────────────────────────────────────────────────────────


def test_check_url_schemes_and_private_hosts() -> None:
    assert check_url("ftp://example.com") is not None
    assert check_url("file:///etc/passwd") is not None
    assert check_url("https://127.0.0.1/x") is not None
    assert check_url("http://localhost:8080") is not None
    assert check_url("https://192.168.1.5/admin") is not None
    assert check_url("https://[::1]/") is not None


# ─── HTML extraction ─────────────────────────────────────────────────────────


def test_html_to_text_extracts_and_links() -> None:
    html = (
        "<html><head><title>My Page</title><style>x{}</style></head>"
        "<body><h1>Hello</h1><script>evil()</script>"
        '<p>See <a href="/docs">the docs</a> now.</p></body></html>'
    )
    title, text = html_to_text(html, "https://example.com/a/b")
    assert title == "My Page"
    assert "# Hello" in text
    assert "the docs (https://example.com/docs)" in text
    assert "evil" not in text and "x{}" not in text


# ─── WebFetch (mock transport) ───────────────────────────────────────────────


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_webfetch_returns_fenced_text() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"  # http upgraded
        return httpx.Response(
            200, headers={"content-type": "text/html"},
            content=b"<html><title>T</title><body><p>" + b"hello world " * 20 + b"</p></body></html>",
        )

    ctx = ToolCtx(extra={"web_transport": _transport(respond)})
    outcome = await fetch_handler({"url": "http://example.com/page"}, ctx)
    assert not outcome.is_error
    assert outcome.text.startswith("https://example.com/page — T")
    assert f'<{UNTRUSTED_TAG} source="WebFetch">' in outcome.text
    assert "hello world" in outcome.text


async def test_webfetch_pages_with_offset() -> None:
    body = ("word " * 3000).encode()

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=body)

    ctx = ToolCtx(extra={"web_transport": _transport(respond)})
    outcome = await fetch_handler(
        {"url": "https://example.com/big.txt", "max_chars": 100}, ctx
    )
    assert "continue with offset=100" in outcome.text
    outcome2 = await fetch_handler(
        {"url": "https://example.com/big.txt", "max_chars": 100, "offset": 100}, ctx
    )
    assert "showing 100..200" in outcome2.text


async def test_webfetch_reports_cross_host_redirect() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://other.org/x"})
        raise AssertionError("cross-host redirect must not be followed")

    ctx = ToolCtx(extra={"web_transport": _transport(respond)})
    outcome = await fetch_handler({"url": "https://example.com/"}, ctx)
    assert not outcome.is_error
    assert "redirected to a different host: https://other.org/x" in outcome.text


async def test_webfetch_http_error_is_in_context_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    ctx = ToolCtx(extra={"web_transport": _transport(respond)})
    outcome = await fetch_handler({"url": "https://example.com/miss"}, ctx)
    assert outcome.is_error and "HTTP 404" in outcome.text


# ─── WebSearch ───────────────────────────────────────────────────────────────

_DDG_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.astral.sh%2Fuv%2F&rut=x">uv docs</a>
  <a class="result__snippet" href="#">An extremely fast <b>Python</b> package manager.</a>
</div>
<div class="result">
  <a class="result__a" href="https://spam.example/page">Spam page</a>
  <a class="result__snippet" href="#">buy things</a>
</div>
"""


def test_parse_search_results_unwraps_uddg() -> None:
    results = parse_search_results(_DDG_HTML)
    assert results[0]["url"] == "https://docs.astral.sh/uv/"
    assert results[0]["title"] == "uv docs"
    assert "Python package manager" in results[0]["snippet"]


async def test_websearch_filters_domains() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_DDG_HTML)

    ctx = ToolCtx(extra={"web_transport": _transport(respond)})
    outcome = await search_handler(
        {"query": "uv", "blocked_domains": ["spam.example"]}, ctx
    )
    assert "docs.astral.sh" in outcome.text
    assert "spam.example" not in outcome.text
    outcome = await search_handler(
        {"query": "uv", "allowed_domains": ["spam.example"]}, ctx
    )
    assert "docs.astral.sh" not in outcome.text


# ─── mid-turn steering ───────────────────────────────────────────────────────


def test_steer_marker_shape_and_prompt_agreement() -> None:
    rendered = format_steer("switch to the staging config")
    assert rendered.startswith(f"\n\n{STEER_OPEN}\n")
    assert rendered.endswith(f"\n{STEER_CLOSE}")
    # The base prompt teaches the exact marker the engine renders.
    from fluid.cli.prompt import DEFAULT_BASE_PROMPT

    assert STEER_OPEN in DEFAULT_BASE_PROMPT
    assert STEER_CLOSE in DEFAULT_BASE_PROMPT


async def test_steer_requires_running_turn() -> None:
    from fluid.engine.native import NativeLoop
    from fluid.store.jsonl import JsonlStore
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        loop = NativeLoop(provider=_NullProvider(), store=JsonlStore(d), model="m")
        with pytest.raises(RuntimeError):
            await loop.submit(Steer(text="too late"))


class _NullProvider:
    name = "null"

    async def stream(self, request):  # pragma: no cover
        if False:
            yield None


# ─── availability probes ─────────────────────────────────────────────────────


async def test_probe_gates_listing_and_dispatch() -> None:
    up = {"value": False}

    async def handler(input, ctx):
        return "ran"

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(
            name="Flaky", description="d", input_schema={"type": "object"},
            check_available=lambda: up["value"],
        ),
        handler,
    )
    clock = {"now": 0.0}
    registry._clock = lambda: clock["now"]

    assert "Flaky" not in [s.name for s in registry.list_tools()]
    outcome = await registry.dispatch("Flaky", {}, ToolCtx())
    assert outcome.is_error and "temporarily unavailable" in outcome.text

    # Comes up: verdict cached for the TTL.
    up["value"] = True
    clock["now"] += PROBE_TTL_S + 1
    assert "Flaky" in [s.name for s in registry.list_tools()]

    # A transient failure within the grace window serves the last-good True.
    up["value"] = False
    clock["now"] += PROBE_TTL_S + 1
    assert registry.available("Flaky") is True
    # Past the grace window, the failure sticks.
    clock["now"] += PROBE_GRACE_S + 1
    assert registry.available("Flaky") is False

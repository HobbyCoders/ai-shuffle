"""Local provider: base_url normalization, request-body shape, client e2e."""

import json

import httpx

from fluid.messages import Message, Text
from fluid.providers.base import ProviderRequest
from fluid.providers.local.client import LocalClient
from fluid.providers.local.serialize import build_request_body, normalize_base_url


# ─── base_url normalization ──────────────────────────────────────────────────


def test_origin_gets_v1_chat_completions() -> None:
    assert (
        normalize_base_url("http://127.0.0.1:8080")
        == "http://127.0.0.1:8080/v1/chat/completions"
    )


def test_ollama_origin() -> None:
    assert (
        normalize_base_url("http://127.0.0.1:11434")
        == "http://127.0.0.1:11434/v1/chat/completions"
    )


def test_v1_prefix_gets_chat_completions() -> None:
    assert (
        normalize_base_url("http://127.0.0.1:8080/v1")
        == "http://127.0.0.1:8080/v1/chat/completions"
    )


def test_full_endpoint_used_verbatim() -> None:
    assert (
        normalize_base_url("http://127.0.0.1:8080/v1/chat/completions")
        == "http://127.0.0.1:8080/v1/chat/completions"
    )


def test_trailing_slashes_ignored() -> None:
    assert (
        normalize_base_url("http://127.0.0.1:8080/v1/")
        == "http://127.0.0.1:8080/v1/chat/completions"
    )
    assert (
        normalize_base_url("http://127.0.0.1:8080/")
        == "http://127.0.0.1:8080/v1/chat/completions"
    )


def test_non_v1_prefix_kept_and_extended() -> None:
    # A proxy path that isn't /v1 gets the full suffix appended.
    assert (
        normalize_base_url("http://host/llama")
        == "http://host/llama/v1/chat/completions"
    )


def test_client_exposes_normalized_url() -> None:
    client = LocalClient(base_url="http://127.0.0.1:8080")
    assert client.url == "http://127.0.0.1:8080/v1/chat/completions"


# ─── request body shape ──────────────────────────────────────────────────────


def _request(**kw) -> ProviderRequest:
    defaults = dict(
        model="qwen2.5-7b-instruct",
        messages=[Message(role="user", content=[Text("hi")])],
    )
    defaults.update(kw)
    return ProviderRequest(**defaults)


def test_body_is_plain_openai_chat_completions() -> None:
    body = build_request_body(_request(system="be terse"))
    assert body["model"] == "qwen2.5-7b-instruct"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    # No OpenRouter-flavored fields.
    assert "usage" not in body
    assert "reasoning" not in body


def test_effort_does_not_emit_reasoning() -> None:
    # Local servers reject/ignore unknown fields inconsistently — the effort
    # knob is deliberately NOT mapped; use extra if a server understands it.
    body = build_request_body(_request(effort="high"))
    assert "reasoning" not in body


def test_extra_merges_last_and_wins() -> None:
    body = build_request_body(
        _request(extra={"cache_prompt": True, "max_tokens": 512, "temperature": 0.2})
    )
    assert body["cache_prompt"] is True
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 512  # extra overrides the default


def test_config_extra_merges_and_request_extra_wins() -> None:
    # Standing (client/config-level) knobs merge in; per-request extra beats them.
    body = build_request_body(
        _request(extra={"temperature": 0.9}),
        extra={"cache_prompt": True, "temperature": 0.2},
    )
    assert body["cache_prompt"] is True
    assert body["temperature"] == 0.9  # request-level wins


def test_tools_translate_to_openai_function_specs() -> None:
    body = build_request_body(
        _request(
            tools=[
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ]
        )
    )
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_no_cache_control_ever() -> None:
    body = build_request_body(_request(system="s", cache_breakpoints=3))
    assert "cache_control" not in json.dumps(body)


# ─── fake-transport end-to-end ───────────────────────────────────────────────

_SSE = (
    'data: {"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n'
    "\n"
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n'
    "\n"
    'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,'
    '"total_tokens":12,"prompt_tokens_details":{"cached_tokens":4}}}\n'
    "\n"
    "data: [DONE]\n"
    "\n"
)


async def test_client_end_to_end_sends_bearer_and_normalizes_usage() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=_SSE.encode(), headers={"content-type": "text/event-stream"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LocalClient(base_url="http://127.0.0.1:8080", http_client=http)
    chunks = [c async for c in client.stream(_request())]
    await http.aclose()

    assert seen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    # Bearer header always sent — placeholder key by default.
    assert seen["auth"] == "Bearer local"
    assert "usage" not in seen["body"]  # no OpenRouter accounting field

    assert chunks[0].kind == "message_start"
    assert chunks[-1].kind == "message_stop"
    text = "".join(c.payload["text"] for c in chunks if c.kind == "text_delta")
    assert text == "Hello"
    usage = next(c for c in chunks if c.kind == "usage").payload["usage"]
    assert usage["input_tokens"] == 6  # 10 prompt - 4 cached
    assert usage["cache_read_input_tokens"] == 4
    assert usage["output_tokens"] == 2
    assert "cost_usd" not in usage


async def test_explicit_api_key_is_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-secret"
        return httpx.Response(
            200, content=_SSE.encode(), headers={"content-type": "text/event-stream"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LocalClient("sk-secret", base_url="http://127.0.0.1:8080", http_client=http)
    chunks = [c async for c in client.stream(_request())]
    await http.aclose()
    assert chunks[-1].kind == "message_stop"


def test_usage_degrades_to_zero() -> None:
    from fluid.providers._chat_completions import normalize_usage

    assert normalize_usage({}) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
    }
    assert normalize_usage({"prompt_tokens": None, "completion_tokens": None}) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens": 0,
    }


# ─── config wiring ───────────────────────────────────────────────────────────


def test_local_kind_registered_and_buildable() -> None:
    from fluid.config.loader import BUILTIN_DEFAULTS, _build_provider_client
    from fluid.config.model import PROVIDER_KINDS, HarnessConfig

    assert "local" in PROVIDER_KINDS
    config = HarnessConfig.from_dict(dict(BUILTIN_DEFAULTS))
    client = _build_provider_client(config, "local")
    assert client.name == "local"
    assert client.url == "http://127.0.0.1:8080/v1/chat/completions"


def test_tool_style_text_wraps_with_adapter() -> None:
    from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge, _build_provider_client
    from fluid.config.model import HarnessConfig
    from fluid.providers.local.text_tools import TextToolsAdapter

    merged = deep_merge(
        BUILTIN_DEFAULTS,
        {"providers": {"local": {"tool_style": "text"}}},
    )
    config = HarnessConfig.from_dict(merged)
    client = _build_provider_client(config, "local")
    assert isinstance(client, TextToolsAdapter)
    assert client.name == "local"


def test_tool_style_validated() -> None:
    import pytest

    from fluid.config.model import ProviderConfig

    with pytest.raises(ValueError, match="tool_style"):
        ProviderConfig.from_dict({"kind": "local", "tool_style": "xml"})


def test_tool_style_round_trips_to_dict() -> None:
    from fluid.config.model import HarnessConfig

    config = HarnessConfig.from_dict(
        {"providers": {"llama": {"kind": "local", "base_url": "http://x", "tool_style": "text"}}}
    )
    assert config.to_dict()["providers"]["llama"]["tool_style"] == "text"


async def test_config_extra_reaches_the_request_body() -> None:
    # [providers.local.extra] in TOML → standing knobs on every request.
    from fluid.config.loader import BUILTIN_DEFAULTS, deep_merge, _build_provider_client
    from fluid.config.model import HarnessConfig

    merged = deep_merge(
        BUILTIN_DEFAULTS,
        {"providers": {"local": {"extra": {"cache_prompt": True}}}},
    )
    config = HarnessConfig.from_dict(merged)
    assert config.to_dict()["providers"]["local"]["extra"] == {"cache_prompt": True}

    client = _build_provider_client(config, "local")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=_SSE.encode(), headers={"content-type": "text/event-stream"}
        )

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async for _ in client.stream(_request()):
        pass
    await client._http.aclose()
    assert seen["body"]["cache_prompt"] is True

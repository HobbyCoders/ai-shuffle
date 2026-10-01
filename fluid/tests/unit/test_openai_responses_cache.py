"""Prompt-cache routing on the ChatGPT Codex backend.

Production sessions under routing arm A sent no per-session header, so every
session shared one route and consecutive calls kept landing on cache nodes
that had never seen the prefix. Arm B (the Codex identity headers) is now the
default on the Codex backend. These tests pin:

* the golden two-call wire: the second request's body starts with the first
  one's bytes, and its headers are the same, identity headers included;
* the default arm per endpoint, and ``FLUID_RESPONSES_ROUTING_ARM`` as the
  switch back to the old wire;
* identity headers per session and role, stable across retries, and never
  sent to the public API;
* the routing headers the cache probe hashes are the ones on the wire;
* ``FLUID_RESPONSES_ROUTING_HINT``, off by default.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from fluid.messages import Message, Reasoning, Text, ToolResultBlock, ToolUseBlock
from fluid.providers._retry import RetryPolicy
from fluid.providers.base import ProviderRequest, WireState
from fluid.providers.openai_responses.client import (
    API_URL,
    CODEX_RESPONSES_URL,
    ROUTING_ARM_ENV,
    ROUTING_HINT_ENV,
    ROUTING_HINT_HEADER,
    OpenAIResponsesClient,
    resolve_routing_arm,
)

MODEL = "gpt-6-astra"
IDENTITY = ("session-id", "thread-id", "x-client-request-id")
# What a Codex-backend request carried before arm B became the default.
CODEX_BASE_HEADERS = {
    "host", "accept", "accept-encoding", "connection", "user-agent",
    "content-length", "content-type", "authorization",
    "chatgpt-account-id", "openai-beta", "originator",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROUTING_ARM_ENV, raising=False)
    monkeypatch.delenv(ROUTING_HINT_ENV, raising=False)


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"


_OK = (
    _event("response.created", {"response": {"id": "resp_1", "status": "in_progress"}})
    + _event("response.output_text.delta", {"item_id": "msg_1", "delta": "hi"})
    + _event("response.output_item.done", {"item": {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }})
    + _event("response.completed", {"response": {
        "id": "resp_1", "status": "completed", "output": [],
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }})
)


class _Auth:
    account_id = "acct_1"

    async def get_access_token(self) -> str:
        return "tok"

    async def on_401(self) -> None:  # pragma: no cover - not hit here
        pass


class _Recorder:
    """MockTransport handler that records requests; ``statuses`` are served
    first, then 200 SSE."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.statuses:
            return httpx.Response(self.statuses.pop(0), content=b'{"error":"boom"}')
        return httpx.Response(
            200, content=_OK.encode(), headers={"content-type": "text/event-stream"},
        )

    def headers(self, n: int) -> dict[str, str]:
        return dict(self.requests[n].headers)

    def body(self, n: int) -> dict[str, Any]:
        return json.loads(self.requests[n].content)


def _client(
    recorder: _Recorder,
    *,
    base_url: str = CODEX_RESPONSES_URL,
    routing_arm: str | None = None,
    api_key: str | None = None,
    retry: RetryPolicy | None = None,
) -> tuple[OpenAIResponsesClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    client = OpenAIResponsesClient(
        api_key=api_key,
        auth=None if api_key else _Auth(),
        base_url=base_url, http_client=http, routing_arm=routing_arm, retry=retry,
    )
    return client, http


_TOOLS = [
    {
        "name": "read_file", "description": "Read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "run", "description": "Run a command",
        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    },
]


def _request(
    messages: list[Message],
    *,
    cache_key: str | None = "ses_A",
    wire: WireState | None = None,
    extra: dict[str, Any] | None = None,
) -> ProviderRequest:
    request_extra: dict[str, Any] = dict(extra or {})
    if cache_key is not None:
        request_extra["prompt_cache_key"] = cache_key
    return ProviderRequest(
        model=MODEL, system="You are a coding agent.", tools=_TOOLS,
        messages=messages, effort="high", extra=request_extra,
        wire_state=wire if wire is not None else WireState("ses_A"),
    )


def _hi() -> list[Message]:
    return [Message(role="user", content=[Text("hi")])]


async def _send(client: OpenAIResponsesClient, request: ProviderRequest) -> None:
    async for _ in client.stream(request):
        pass


# ─── Golden two-call wire ────────────────────────────────────────────────────


async def test_the_second_call_extends_the_first_byte_for_byte() -> None:
    """The mandatory golden check: consecutive calls of one session send the
    same headers and a body whose bytes before the new input items are the
    previous body's, so the backend can serve the previous prompt from cache.
    """
    recorder = _Recorder()
    client, http = _client(recorder)
    assert client.routing_arm == "B"
    wire = WireState("ses_A")
    turn1 = _hi()
    await _send(client, _request(turn1, wire=wire))

    assistant = Message(role="assistant", content=[
        Reasoning(
            origin="openai_responses", model=MODEL,
            raw={
                "id": "rs_1", "type": "reasoning", "status": "completed",
                "encrypted_content": "BLOB",
                "summary": [{"type": "summary_text", "text": "thinking"}],
                "content": [],
            },
        ),
        Text("Let me look."),
        ToolUseBlock(id="call_1", name="read_file", input={"path": "a.py"}),
    ])
    turn2 = turn1 + [
        assistant,
        Message(role="user", content=[
            ToolResultBlock(tool_use_id="call_1", content=[Text("print(1)")]),
        ]),
        Message(role="user", content=[Text("now run it")]),
    ]
    await _send(client, _request(turn2, wire=wire))
    await http.aclose()

    body1, body2 = recorder.body(0), recorder.body(1)
    assert list(body1) == list(body2), "key order is part of the prefix"
    assert {k: v for k, v in body1.items() if k != "input"} == {
        k: v for k, v in body2.items() if k != "input"
    }
    assert len(body2["input"]) > len(body1["input"])
    assert json.dumps(body2["input"][:len(body1["input"])]) == json.dumps(body1["input"])
    for old, new in zip(body1["input"], body2["input"]):
        assert json.dumps(new) == json.dumps(old)

    raw1, raw2 = recorder.requests[0].content, recorder.requests[1].content
    head1, _, _ = raw1.partition(b'"input":')
    head2, _, _ = raw2.partition(b'"input":')
    assert head1 == head2
    # The first call's serialized input items, without the closing bracket,
    # open the second call's input array.
    input1 = json.dumps(body1["input"], separators=(",", ":"), ensure_ascii=False)
    input2 = json.dumps(body2["input"], separators=(",", ":"), ensure_ascii=False)
    assert input2.startswith(input1[:-1])

    sent1 = {k: v for k, v in recorder.headers(0).items() if k != "content-length"}
    sent2 = {k: v for k, v in recorder.headers(1).items() if k != "content-length"}
    assert sent1 == sent2
    for name in IDENTITY:
        assert sent1[name] == "ses_A"


# ─── Default arm per endpoint, and the switch back ───────────────────────────


def test_the_default_arm_depends_on_the_endpoint() -> None:
    codex = OpenAIResponsesClient(auth=_Auth(), base_url=CODEX_RESPONSES_URL)
    assert codex.routing_arm == "B"
    assert OpenAIResponsesClient(api_key="k", base_url=API_URL).routing_arm == "A"
    assert OpenAIResponsesClient(api_key="k").routing_arm == "A"
    assert OpenAIResponsesClient(
        auth=_Auth(), base_url="https://chatgpt.com/backend-api/codex/v2/responses",
    ).routing_arm == "B"


def test_resolve_routing_arm_falls_back_to_the_given_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert resolve_routing_arm(None) == "A"
    assert resolve_routing_arm(None, "B") == "B"
    assert resolve_routing_arm("  ", "B") == "B"
    assert resolve_routing_arm(" c ", "B") == "C"
    with caplog.at_level(logging.WARNING):
        assert resolve_routing_arm("Z", "B") == "B"
    assert any("using B" in r.getMessage() for r in caplog.records)


async def test_arm_a_from_the_environment_restores_the_old_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ROUTING_ARM_ENV, "A")
    recorder = _Recorder()
    client, http = _client(recorder)
    assert client.routing_arm == "A"
    await _send(client, _request(_hi()))
    await http.aclose()
    sent = recorder.headers(0)
    assert set(sent) == CODEX_BASE_HEADERS
    for name in IDENTITY:
        assert name not in sent


def test_the_environment_and_the_argument_still_choose_the_arm(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(ROUTING_ARM_ENV, "c")
    assert OpenAIResponsesClient(auth=_Auth(), base_url=CODEX_RESPONSES_URL).routing_arm == "C"
    monkeypatch.setenv(ROUTING_ARM_ENV, "Z")
    with caplog.at_level(logging.WARNING):
        codex = OpenAIResponsesClient(auth=_Auth(), base_url=CODEX_RESPONSES_URL)
    assert codex.routing_arm == "B"
    assert any(ROUTING_ARM_ENV in r.getMessage() for r in caplog.records)
    monkeypatch.setenv(ROUTING_ARM_ENV, "C")
    explicit = OpenAIResponsesClient(
        auth=_Auth(), base_url=CODEX_RESPONSES_URL, routing_arm="A",
    )
    assert explicit.routing_arm == "A", "an explicit argument beats the environment"


# ─── Identity per session and role ───────────────────────────────────────────


async def test_each_session_and_role_gets_its_own_route() -> None:
    recorder = _Recorder()
    client, http = _client(recorder)
    await _send(client, _request(_hi(), cache_key="ses_A", wire=WireState("ses_A")))
    await _send(client, _request(_hi(), cache_key="ses_B", wire=WireState("ses_B")))
    await _send(client, _request(
        _hi(), cache_key="learner:ses_A", wire=WireState("ses_A", role="learner"),
    ))
    # The sticky key was dropped: the wire state's session still routes.
    await _send(client, _request(_hi(), cache_key=None, wire=WireState("ses_A")))
    await http.aclose()

    main_a, main_b, learner, dropped = (recorder.headers(i) for i in range(4))
    assert main_a["session-id"] == "ses_A"
    assert main_b["session-id"] == "ses_B"
    assert learner["session-id"] == "learner:ses_A"
    assert learner["thread-id"] == "learner:ses_A"
    assert learner["x-client-request-id"] == "learner:ses_A"
    assert dropped["session-id"] == dropped["thread-id"] == "ses_A"


async def test_a_retry_sends_the_same_identity_headers() -> None:
    recorder = _Recorder(statuses=[500])
    client, http = _client(
        recorder, retry=RetryPolicy(max_retries=2, base_delay=0, jitter=0),
    )
    await _send(client, _request(_hi()))
    await http.aclose()

    assert len(recorder.requests) == 2
    first, retry = recorder.headers(0), recorder.headers(1)
    for name in IDENTITY:
        assert first[name] == retry[name] == "ses_A"


async def test_the_public_api_never_sees_codex_headers() -> None:
    recorder = _Recorder()
    client, http = _client(recorder, base_url=API_URL, api_key="sk-test")
    assert client.routing_arm == "A"
    request = _request(_hi())
    assert client.routing_headers(request) == {}
    await _send(client, request)
    await http.aclose()
    sent = recorder.headers(0)
    for name in ("chatgpt-account-id", *IDENTITY, ROUTING_HINT_HEADER):
        assert name not in sent


async def test_the_probe_hashes_the_headers_that_are_sent() -> None:
    recorder = _Recorder()
    client, http = _client(recorder)
    request = _request(_hi())
    routing = client.routing_headers(request)
    await _send(client, request)
    await http.aclose()
    sent = recorder.headers(0)
    assert {"session-id", "thread-id", "chatgpt-account-id"} <= set(routing)
    assert all(sent[name.lower()] == value for name, value in routing.items())


# ─── Routing hint (off by default) ───────────────────────────────────────────


async def test_the_routing_hint_is_off_by_default() -> None:
    recorder = _Recorder()
    client, http = _client(recorder)
    request = _request(_hi())
    await _send(client, request)
    await http.aclose()
    assert ROUTING_HINT_HEADER not in recorder.headers(0)
    assert ROUTING_HINT_HEADER not in client.routing_headers(request)


async def test_the_routing_hint_names_the_model_and_tier_when_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ROUTING_HINT_ENV, "1")
    recorder = _Recorder()
    client, http = _client(recorder)
    plain = _request(_hi())
    tiered = _request(_hi(), extra={"service_tier": "priority"})
    await _send(client, plain)
    await _send(client, tiered)
    await http.aclose()

    assert recorder.headers(0)[ROUTING_HINT_HEADER] == f"model={MODEL}"
    assert client.routing_headers(plain)[ROUTING_HINT_HEADER] == f"model={MODEL}"
    expected = f"model={MODEL};tier=priority"
    assert recorder.headers(1)[ROUTING_HINT_HEADER] == expected
    assert client.routing_headers(tiered)[ROUTING_HINT_HEADER] == expected


@pytest.mark.parametrize("value", ["true", "YES"])
def test_the_routing_hint_accepts_truthy_words(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv(ROUTING_HINT_ENV, value)
    client = OpenAIResponsesClient(auth=_Auth(), base_url=CODEX_RESPONSES_URL)
    assert client.routing_headers(_request(_hi()))[ROUTING_HINT_HEADER] == f"model={MODEL}"


async def test_the_routing_hint_stays_off_under_arm_a_and_off_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ROUTING_HINT_ENV, "1")
    arm_a_recorder, api_recorder = _Recorder(), _Recorder()
    arm_a, arm_a_http = _client(arm_a_recorder, routing_arm="A")
    api, api_http = _client(api_recorder, base_url=API_URL, api_key="sk-test")
    request = _request(_hi())
    await _send(arm_a, request)
    await _send(api, request)
    await arm_a_http.aclose()
    await api_http.aclose()

    assert ROUTING_HINT_HEADER not in arm_a_recorder.headers(0)
    assert ROUTING_HINT_HEADER not in api_recorder.headers(0)
    assert ROUTING_HINT_HEADER not in arm_a.routing_headers(request)
    assert api.routing_headers(request) == {}

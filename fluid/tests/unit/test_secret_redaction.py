"""Credentials stay out of provider errors, ``fluid config`` and learner evidence."""
import json

import httpx
import pytest

from fluid.cli.__main__ import _redacted_config
from fluid.codemode.continual import Synthesizer
from fluid.codemode.journal import ToolJournal
from fluid.codemode.skills import SkillStore
from fluid.config.model import HarnessConfig
from fluid.context.screening import redact_text
from fluid.providers._chat_completions import stream_chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers.anthropic.client import AnthropicClient
from fluid.providers.base import ProviderRequest
from fluid.providers.gemini.client import GeminiClient
from fluid.providers.openai_responses.client import OpenAIResponsesClient

# No vendor prefix: only the exact-value pass can catch it.
KEY = "plainkey0123456789abcdefXYZ"
URL = "https://provider.example.com/v1/endpoint"
REQUEST = ProviderRequest(model="test-model", messages=[])
NO_RETRY = RetryPolicy(max_retries=0, base_delay=0)


def test_redact_text_known_values_shapes_and_clipped_prefix():
    text = (
        f"echo {KEY}; Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345 "
        "sk-ant-api03-abcdefghijklmnop https://user:hunter2pass@db.example.com/x "
        "api_key=ABCDEFGHIJKLMNOPQRST ordinary words stay"
    )
    out = redact_text(text, [KEY])
    for secret in (KEY, "abcdefghijklmnopqrstuvwxyz012345", "sk-ant-api03", "hunter2pass",
                   "ABCDEFGHIJKLMNOPQRST"):
        assert secret not in out
    assert "Bearer [redacted]" in out and "https://user:[redacted]@db.example.com" in out
    assert out.endswith("ordinary words stay")
    # A clip that cut the key in half leaves no prefix of it.
    assert redact_text("bad key " + KEY[:15] + "…", [KEY]) == "bad key [redacted]…"
    assert redact_text("bad key " + KEY[:12], [KEY]) == "bad key [redacted]"
    # A short local placeholder key is not a credential and garbles nothing.
    assert redact_text("local model loaded", ["local"]) == "local model loaded"


def _refusing(status, body):
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=body)))


def _sse(*events):
    data = b"".join(f"event: {n}\ndata: {json.dumps(d)}\n\n".encode() for n, d in events)
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=data,
                                       headers={"content-type": "text/event-stream"})))


def _stream(kind, http):
    if kind == "responses":
        return OpenAIResponsesClient(api_key=KEY, base_url=URL, http_client=http, retry=NO_RETRY).stream(REQUEST)
    if kind == "anthropic":
        return AnthropicClient(api_key=KEY, base_url=URL, http_client=http, retry=NO_RETRY).stream(REQUEST)
    if kind == "gemini":
        return GeminiClient(api_key=KEY, base_url=URL, http_client=http, retry=NO_RETRY).stream(REQUEST)
    return stream_chat_completions(URL, {"model": "test-model", "messages": []},
                                   {"Authorization": f"Bearer {KEY}"}, http_client=http,
                                   timeout=5, retry=NO_RETRY)


def _errors_text(chunks):
    errors = [c.payload for c in chunks if c.kind == "error"]
    assert errors
    return json.dumps(errors)


@pytest.mark.parametrize("kind", ["responses", "anthropic", "gemini", "chat"])
async def test_http_refusal_that_echoes_the_key_is_redacted(kind):
    # A proxy quoting the rejected header back, once in full and once where
    # the 2000-character message clip cuts it in half.
    body = f"Unauthorized: header Authorization: Bearer {KEY} rejected. " + "x" * 1900 + KEY
    async with _refusing(401, body) as http:
        chunks = [c async for c in _stream(kind, http)]
    text = _errors_text(chunks)
    assert KEY[:10] not in text
    assert "[redacted]" in text


async def test_anthropic_stream_error_redacted_on_the_first_party_lane():
    """Only the compatible-provider lane used to redact in-stream errors."""
    async with _sse(("error", {"type": "error", "error": {
        "type": "invalid_request_error", "message": f"bad x-api-key {KEY}"}})) as http:
        chunks = [c async for c in _stream("anthropic", http)]
    assert KEY not in _errors_text(chunks)


async def test_responses_stream_error_redacts_a_fetched_oauth_token():
    token = "oauthtoken-ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    class Auth:
        async def get_access_token(self):
            return token

        async def on_401(self):
            pass

    async with _sse(("error", {"error": {"message": f"token {token} expired " + "y" * 600 + token}})) as http:
        client = OpenAIResponsesClient(auth=Auth(), base_url=URL, http_client=http, retry=NO_RETRY)
        chunks = [c async for c in client.stream(REQUEST)]
    text = _errors_text(chunks)
    assert token[:10] not in text


def test_fluid_config_view_masks_credentials_but_round_trip_keeps_them():
    config = HarnessConfig.from_dict({
        "providers": {"proxy": {"kind": "local",
                                "base_url": "https://user:hunter2pass@llm.example.com/v1",
                                "api_key_env": "PROXY_KEY"}},
        "mcp": {"tracker": {"url": "https://mcp.example.com",
                            "headers": {"Authorization": f"Bearer {KEY}"},
                            "env": {"TRACKER_TOKEN": KEY},
                            "args": ["--token", "ghp_abcdefghijklmnopqrstuvwxyz0123"]}},
    })
    raw = config.to_dict()
    assert raw["mcp"]["tracker"]["env"]["TRACKER_TOKEN"] == KEY  # serializer unchanged
    shown = _redacted_config(raw)
    text = json.dumps(shown)
    assert KEY not in text and "hunter2pass" not in text and "ghp_abc" not in text
    assert shown["mcp"]["tracker"]["headers"] == {"Authorization": "[redacted]"}
    assert shown["mcp"]["tracker"]["env"] == {"TRACKER_TOKEN": "[redacted]"}
    assert shown["providers"]["proxy"]["api_key_env"] == "PROXY_KEY"  # a name, not a secret
    assert shown["mcp"]["tracker"]["url"] == "https://mcp.example.com"


def test_learner_evidence_redacted_on_write_and_when_rendered(tmp_path):
    store = SkillStore(tmp_path / "skills")
    # Assembled so the repository's secret scan does not flag a fake key.
    secret = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    journal = ToolJournal(store)
    journal.record_tool("Bash", {"command": f"curl -H 'Authorization: Bearer {secret}'"},
                        f"export OPENAI_API_KEY={secret}", False)
    journal.record_event("user", f"my key is {secret}")
    on_disk = store._journal_path.read_text(encoding="utf-8")
    assert secret[:12] not in on_disk and "[redacted]" in on_disk
    # A journal written before redaction still reaches the learner clean.
    legacy = {"kind": "tool", "name": "Bash", "input": f'{{"token": "{secret}"}}',
              "output": f"password={secret}"}
    rendered = "\n".join(Synthesizer._render_entry(legacy, 0))
    rendered += "\n".join(Synthesizer._render_entry(
        {"kind": "cell", "code": f"key = '{secret}'", "stdout": secret}, 1))
    assert secret[:12] not in rendered

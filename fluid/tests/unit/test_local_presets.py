"""Wire evidence for opt-in candidates, not local quality/performance evidence."""
import json

import httpx
import pytest

from fluid.providers.base import ProviderRequest
from fluid.providers.local.client import LocalClient
from fluid.providers.local.presets import candidate_extra


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning,temp,top_p", [(True,.6,.95), (False,.7,.8)])
async def test_qwen3_candidate_wire(reasoning, temp, top_p):
    captured = []
    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type":"text/event-stream"},
                              text="data: [DONE]\n\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = LocalClient(base_url="http://fixture.invalid", http_client=http,
                             extra=candidate_extra("qwen3", reasoning=reasoning, dialect="llama.cpp"))
        async for _ in client.stream(ProviderRequest(model="local-qwen3", messages=[], effort="high")):
            pass
    body = captured[0]
    assert body["temperature"] == temp
    assert body["top_p"] == top_p
    assert body["top_k"] == 20
    assert body["min_p"] == 0.0
    assert body["chat_template_kwargs"] == {"enable_thinking": reasoning}
    assert "reasoning" not in body  # a local template switch is not API effort


@pytest.mark.parametrize("family,dialect", [("unknown","llama.cpp"), ("qwen3","ollama")])
def test_no_guessed_family_or_server(family, dialect):
    with pytest.raises(ValueError):
        candidate_extra(family, reasoning=True, dialect=dialect)

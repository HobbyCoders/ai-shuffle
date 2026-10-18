"""Host-only memory pin provenance survives stores, not provider serialization."""
import json
from dataclasses import replace

import pytest

from fluid.messages import Message, Text
from fluid.providers.base import ProviderRequest
from fluid.providers.anthropic.serialize import build_request_body as anthropic
from fluid.providers.openai_responses.serialize import build_request_body as responses
from fluid.providers.openrouter.serialize import build_request_body as openrouter
from fluid.providers.local.serialize import build_request_body as local
from fluid.store.base import SessionNode
from fluid.store.jsonl import JsonlStore


@pytest.mark.parametrize("serialize", [anthropic, responses, openrouter, local])
def test_pin_never_changes_provider_bytes(serialize):
    note = "Trusted host context"
    message = Message(role="user", content=[Text("quoted prompt\n\n" + note), Text("\nextra hook")])
    request = ProviderRequest(model="test-model", messages=[message], system="stable prefix")
    before = serialize(request)
    message.pin_context(note)
    assert message.pinned_text() == note
    assert serialize(request) == before
    decoded = Message.from_dict(json.loads(json.dumps(message.to_dict())))
    assert decoded.context_pin == message.context_pin
    assert serialize(replace(request, messages=[decoded])) == before


@pytest.mark.parametrize("pin", [None, (-1, 2), (0, 999), (2, 1), (True, 2), ("0", 2), (1,)])
def test_unmarked_or_invalid_ranges_have_no_provenance(pin):
    assert Message(role="user", content=[Text("raw quote")], context_pin=pin).pinned_text() == ""


@pytest.mark.asyncio
async def test_pin_survives_jsonl_restart(tmp_path):
    store = JsonlStore(tmp_path)
    session = await store.create_session()
    note = "the appended note"
    message = Message(role="user", content=[Text("raw owner bytes\n\n" + note)]).pin_context(note)
    await store.append(session.session_id, SessionNode(node_id="pinned", parent_id=None, message=message))
    reloaded = await JsonlStore(tmp_path).load_path(session.session_id)
    assert reloaded[0].message.pinned_text() == note
    assert reloaded[0].message.to_dict() == message.to_dict()

import json

import pytest

from scripts.devloop.peer_fixture import PeerFixture


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress,action,original,expected", [
    ("remote", "hydrate", 200, b"partial"), ("remote", "other", 200, b"ordinary"),
    ("preview", "hydrate", 200, b"ordinary"), ("remote", "hydrate", 403, b"ordinary"),
])
async def test_only_replaces_success_for_selected_peer_action(tmp_path, ingress, action, original, expected):
    (tmp_path / "reply.json").write_text(json.dumps({"action": "hydrate", "status": 202, "body": "partial"}))
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": original, "headers": []})
        await send({"type": "http.response.body", "body": b"ordinary"})

    async def collect(message):
        messages.append(message)

    await PeerFixture(app, tmp_path)({"type": "http", "method": "POST", "shuffle.ingress": ingress,
                                     "path": f"/api/v1/network/peer/control/{action}"}, None, collect)
    assert messages[-1]["body"] == expected
    assert messages[0]["status"] == (202 if expected == b"partial" else original)

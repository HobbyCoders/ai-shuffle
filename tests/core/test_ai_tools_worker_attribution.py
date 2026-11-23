"""The direct fal tools name the worker that ran them.

They bill this worker's own FAL_KEY. After a chat is handed off, the usage
summary must not pin an earlier charge on the new worker's fal balance, so each
result names the worker the way a generation choice does.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.mcp.ai_tools_sdk import _create_image_tools, _FalToolWorker
from app.mcp.node_worker import NodeWorker


def _edit(result, device_id):
    worker = _FalToolWorker(NodeWorker(worker_script="worker.js"))
    tools = {t.name: t for t in _create_image_tools(worker)}
    with patch.object(NodeWorker, "call", AsyncMock(return_value=result)), \
            patch("app.network.runners.self_device_id", return_value=device_id):
        out = asyncio.run(tools["editImage"].handler({"prompt": "p", "image_path": "a.png"}))
    return json.loads(out["content"][0]["text"])


def test_a_network_member_names_its_device():
    body = _edit({"success": True, "provider_used": "fal-ai"}, "dev_here")
    assert body["worker"] == "dev_here"
    assert body["provider_used"] == "fal-ai"


def test_a_worker_on_no_network_names_local():
    assert _edit({"success": True, "provider_used": "fal-ai"}, None)["worker"] == "local"


def test_a_result_that_already_names_a_worker_keeps_it():
    assert _edit({"success": True, "worker": "dev_other"}, "dev_here")["worker"] == "dev_other"


def test_attribution_failure_never_fails_the_call():
    with patch("app.core.generation_choices._local_worker", side_effect=RuntimeError("down")):
        body = _edit({"success": True, "provider_used": "fal-ai"}, "dev_here")
    assert body == {"success": True, "provider_used": "fal-ai"}

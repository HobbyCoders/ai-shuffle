"""The fal model ID reaches the Node tools as ``model`` (#1398).

The Node tools ignore ``provider`` (fal is the only provider) and read
``model``. The schemas used to advertise the model ID as ``provider``, so a
requested model silently fell back to the configured default one.
"""

import asyncio

import pytest

from app.mcp.ai_tools_sdk import _create_image_tools, _create_video_tools


class _Worker:
    def __init__(self):
        self.calls = []

    async def call(self, method, args):
        self.calls.append((method, args))
        return {"success": True}


def _tools(worker):
    return {t.name: t for t in [*_create_image_tools(worker), *_create_video_tools(worker)]}


MODEL_TOOLS = ["generateImage", "editImage", "generateVideo", "imageToVideo"]


@pytest.mark.parametrize("name", MODEL_TOOLS)
def test_schema_names_the_model_argument_the_worker_reads(name):
    schema = _tools(_Worker())[name].input_schema
    assert "model" in schema["properties"]
    assert "provider" not in schema["properties"]


@pytest.mark.parametrize("name", MODEL_TOOLS)
@pytest.mark.parametrize("args,expected", [
    ({"prompt": "p", "model": "fal-ai/flux/schnell"}, {"prompt": "p", "model": "fal-ai/flux/schnell"}),
    # A call made against the old schema keeps its requested model.
    ({"prompt": "p", "provider": "fal-ai/flux/schnell"}, {"prompt": "p", "model": "fal-ai/flux/schnell"}),
    ({"prompt": "p", "provider": "fal-ai/flux/dev", "model": "fal-ai/flux/schnell"},
     {"prompt": "p", "model": "fal-ai/flux/schnell"}),
    ({"prompt": "p", "provider": "fal-ai"}, {"prompt": "p"}),
])
def test_worker_receives_the_requested_model(name, args, expected):
    worker = _Worker()
    asyncio.run(_tools(worker)[name].handler(dict(args)))
    assert worker.calls == [(name, expected)]

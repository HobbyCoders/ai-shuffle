"""Native image tools retain their wire type and final artifacts survive SSE."""

from fluid.messages import Message, Text
from fluid.providers.base import ProviderRequest
from fluid.providers.openai_responses.serialize import (
    ResponsesStreamNormalizer,
    build_request_body,
)


def test_native_image_tool_is_not_converted_to_a_function_or_inferred_from_model_name():
    native = {"type": "image_generation", "model": "requested-image-model",
              "quality": "high", "output_format": "png", "partial_images": 2}
    request = ProviderRequest(model="configured-model", messages=[Message("user", [Text("Draw a tree")])],
                              tools=[{"name": "Read", "input_schema": {"type": "object"}}, native])
    body = build_request_body(request)
    assert body["model"] == "configured-model"
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][1] == native
    assert body["tools"][1] is not native
    assert "name" not in body["tools"][1]


def test_function_named_image_generation_is_still_a_function():
    request = ProviderRequest(model="configured", messages=[], tools=[{"name": "image_generation"}])
    assert build_request_body(request)["tools"][0]["type"] == "function"


def image_item(**overrides):
    return {"id": "ig_test", "type": "image_generation_call", "status": "completed",
            "result": "synthetic-base64", "output_format": "png", "revised_prompt": "A tree", **overrides}


def test_partial_previews_and_progress_cannot_be_mistaken_for_final_artifacts():
    normalizer = ResponsesStreamNormalizer()
    for name in ("in_progress", "generating", "partial_image", "completed"):
        chunks = normalizer.consume("response.image_generation_call." + name,
                                    {"item_id": "ig_test", "partial_image_b64": "preview", "partial_image_index": 0})
        assert len(chunks) == 1 and chunks[0].kind == "image_generation_progress"
    final, = normalizer.consume("response.output_item.done", {"item": image_item()})
    assert final.kind == "image_generation"
    assert final.payload["result"] == "synthetic-base64"
    assert final.payload["revised_prompt"] == "A tree"
    assert final.payload["status"] == "completed"


def test_completed_response_recovers_images_and_deduplicates_done_items():
    normalizer = ResponsesStreamNormalizer()
    assert normalizer.consume("response.output_item.done", {"item": image_item()})
    chunks = normalizer.consume("response.completed", {"response": {
        "id": "resp_test", "status": "completed", "usage": {"output_tokens": 12},
        "output": [image_item(), image_item(id="ig_second", result="second-image")],
    }})
    images = [c for c in chunks if c.kind == "image_generation"]
    assert len(images) == 1
    assert images[0].payload["item_id"] == "ig_second"
    assert any(c.kind == "usage" and c.payload["usage"]["output_tokens"] == 12 for c in chunks)
    assert any(c.kind == "message_stop" for c in chunks)


def test_incomplete_item_is_not_a_success_and_does_not_hide_later_final_image():
    normalizer = ResponsesStreamNormalizer()
    failed, = normalizer.consume("response.output_item.done", {"item": image_item(status="failed", result=None)})
    assert failed.payload["status"] == "failed" and failed.payload["result"] is None
    chunks = normalizer.consume("response.completed", {"response": {"status": "completed", "output": [image_item()]}})
    assert any(c.kind == "image_generation" and c.payload["result"] == "synthetic-base64" for c in chunks)

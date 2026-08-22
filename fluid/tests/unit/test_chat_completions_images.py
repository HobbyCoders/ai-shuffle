"""Chat-completions translation: tool-result image handling.

Regression: an Image block inside a ToolResultBlock was serialized with
json.dumps(c.to_dict()) into the role:"tool" message text — the model received
hundreds of KB of base64 as TEXT instead of an image part.
"""

import json

from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers._chat_completions import (
    tool_result_content,
    tool_result_text,
    translate_message,
)


def test_text_only_tool_result_is_plain_string() -> None:
    msg = Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=[Text("done")]),
    ])
    out = translate_message(msg)
    assert len(out) == 1
    assert out[0]["role"] == "tool"
    assert out[0]["content"] == "done"


def test_tool_result_image_becomes_image_url_part_not_base64_text() -> None:
    msg = Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=[
            Text("screenshot taken"),
            Image(media_type="image/png", data="aGk="),
        ]),
    ])
    out = translate_message(msg)
    assert len(out) == 1 and out[0]["role"] == "tool"
    content = out[0]["content"]
    assert isinstance(content, list)
    kinds = [part["type"] for part in content]
    assert kinds == ["text", "image_url"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,aGk=")
    # The raw base64 must never leak into the TEXT part.
    assert "aGk=" not in content[0]["text"]
    assert "kind" not in json.dumps(content[0])


def test_tool_result_text_helper_never_dumps_base64() -> None:
    block = ToolResultBlock(tool_use_id="t1", content=[
        Text("note"),
        Image(media_type="image/png", data="a" * 10_000),
    ])
    text = tool_result_text(block)
    assert "a" * 100 not in text
    assert "[image attached]" in text


def test_user_message_images_still_coalesce_into_multimodal_content() -> None:
    msg = Message(role="user", content=[
        Text("look at this"),
        Image(media_type="image/jpeg", data="aGk="),
    ])
    out = translate_message(msg)
    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look at this"}
    assert content[1]["type"] == "image_url"

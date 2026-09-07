"""Chat-completions translation: tool-result image handling.

Regression 1: an Image block inside a ToolResultBlock was serialized with
json.dumps(c.to_dict()) into the role:"tool" message text — the model received
hundreds of KB of base64 as TEXT instead of an image part.

Regression 2: the fix for (1) put image_url parts INSIDE the role:"tool"
message. The chat/completions schema says tool content is text; proxies forward
the parts anyway and upstream then answers HTTP 200 with an empty stream for
the rest of the conversation. Images now ride the user message that follows.
"""

from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers._chat_completions import (
    tool_result_text,
    translate_message,
)

MARKER = "[image attached — see the user message that follows]"


def test_text_only_tool_result_is_plain_string() -> None:
    msg = Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=[Text("done")]),
    ])
    out = translate_message(msg)
    assert len(out) == 1
    assert out[0]["role"] == "tool"
    assert out[0]["content"] == "done"


def test_tool_result_image_rides_a_following_user_message() -> None:
    msg = Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=[
            Text("screenshot taken"),
            Image(media_type="image/png", data="aGk="),
        ]),
    ])
    out = translate_message(msg)
    assert len(out) == 2

    tool_msg, user_msg = out
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "t1"
    # Tool content stays a plain string: text plus a marker where the image was.
    assert isinstance(tool_msg["content"], str)
    assert tool_msg["content"] == "screenshot taken" + MARKER
    # The raw base64 must never leak into the tool message.
    assert "aGk=" not in tool_msg["content"]

    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert content[0]["text"] == "Image from tool result t1:"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,aGk=")


def test_only_the_tool_result_with_an_image_is_referenced() -> None:
    msg = Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=[Text("no picture here")]),
        ToolResultBlock(tool_use_id="t2", content=[
            Text("screenshot taken"),
            Image(media_type="image/png", data="aGk="),
        ]),
    ])
    out = translate_message(msg)
    assert len(out) == 3
    assert [m["role"] for m in out] == ["tool", "tool", "user"]
    assert [m["tool_call_id"] for m in out[:2]] == ["t1", "t2"]
    assert out[0]["content"] == "no picture here"
    assert out[1]["content"] == "screenshot taken" + MARKER

    content = out[2]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert content[0]["text"] == "Image from tool result t2:"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,aGk=")


def test_tool_result_image_and_own_text_share_one_user_message() -> None:
    msg = Message(role="user", content=[
        ToolResultBlock(tool_use_id="t1", content=[
            Image(media_type="image/png", data="aGk="),
        ]),
        Text("what do you see?"),
    ])
    out = translate_message(msg)
    assert len(out) == 2
    assert out[0]["role"] == "tool"
    assert out[0]["content"] == MARKER

    assert out[1]["role"] == "user"
    content = out[1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url", "text"]
    assert content[0]["text"] == "Image from tool result t1:"
    assert content[2]["text"] == "what do you see?"


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


def test_assistant_message_with_tool_calls_is_untouched() -> None:
    msg = Message(role="assistant", content=[
        Text("taking a look"),
        ToolUseBlock(id="t1", name="screenshot", input={"full": True}),
    ])
    out = translate_message(msg)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "taking a look"
    assert out[0]["tool_calls"][0]["id"] == "t1"
    assert out[0]["tool_calls"][0]["function"]["name"] == "screenshot"

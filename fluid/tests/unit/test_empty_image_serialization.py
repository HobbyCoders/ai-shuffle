"""Old transcripts with empty images must remain serializable (issue #2328)."""

import pytest

from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers._chat_completions import translate_message
from fluid.providers.anthropic.serialize import build_request_body as anthropic_body
from fluid.providers.base import ProviderRequest
from fluid.providers.openai_responses.serialize import build_request_body as responses_body


def _dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dicts(child)


@pytest.mark.parametrize("data", ["", " ", "\t\r\n"])
@pytest.mark.parametrize("in_tool_result", [False, True])
@pytest.mark.parametrize("provider", ["responses", "chat", "anthropic"])
def test_empty_image_becomes_text(provider, in_tool_result, data):
    image = Image(media_type="image/jpeg", data=data)
    messages = [Message(role="user", content=[Text("inspect this")])]
    if in_tool_result:
        messages.extend([
            Message(role="assistant", content=[
                ToolUseBlock(id="t1", name="Read", input={"file_path": "/empty.jpg"}),
            ]),
            Message(role="user", content=[
                ToolResultBlock(tool_use_id="t1", content=[Text("caption"), image]),
            ]),
        ])
    else:
        messages.append(Message(role="user", content=[Text("caption"), image]))
    messages.append(Message(role="user", content=[Text("continue")]))
    request = ProviderRequest(model="m", messages=messages)
    if provider == "chat":
        body = [part for message in messages for part in translate_message(message)]
        text_type = "text"
    else:
        body = (responses_body if provider == "responses" else anthropic_body)(request)
        text_type = "input_text" if provider == "responses" else "text"

    parts = list(_dicts(body))
    assert not any(part.get("type") in {"image", "input_image", "image_url"} for part in parts)
    assert any(
        part.get("type") == text_type and part.get("text") == "[image omitted: empty data]"
        for part in parts
    )
    assert image.data == data  # Serialization must not rewrite stored history.

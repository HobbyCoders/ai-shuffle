"""Lossless text paging is separate from bounded multi-message previews (#682)."""
import re

import pytest

from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode
from fluid.store.jsonl import JsonlStore
from fluid.tools.builtins import session_view
from fluid.tools.registry import ToolCtx


@pytest.mark.parametrize("length", [700, 701, 5_000, 40_000])
async def test_reconstruct_message_through_bounded_text_pages(tmp_path, length):
    store = JsonlStore(tmp_path)
    meta = await store.create_session()
    text = "  " + ("line 🦋\n" * length)[:length] + "\nfinal bullet: keep this too  "
    await store.append(meta.session_id, SessionNode(node_id="long", parent_id=None,
        message=Message(role="assistant", content=[Text(text=text)])))
    ctx = ToolCtx(extra={"session_view": store.session_slice})
    preview = await session_view.handler({"session_id": meta.session_id, "count": 1}, ctx)
    assert "text preview clipped" in preview.text
    assert "offset=0, count=1, text_offset=0" in preview.text
    assert "output budget reached" not in preview.text

    parts, position = [], 0
    while True:
        result = await session_view.handler({"session_id": meta.session_id,
            "offset": 0, "text_offset": position}, ctx)
        assert not result.is_error, result.text
        assert len(result.text) <= session_view._OUTPUT_CAP
        body, footer = result.text.split("\n[0 assistant] ", 1)[1].rsplit("\n[", 1)
        parts.append(body)
        continuation = re.search(r"text_offset=(\d+)", footer)
        if continuation is None:
            assert footer == "end of message text]"
            break
        next_position = int(continuation[1])
        assert next_position > position
        position = next_position
    assert "".join(parts) == text
    assert (len(parts) > 1) is (len(text) > 16_000)


async def test_text_page_selects_one_message_and_validates_ranges(tmp_path):
    store = JsonlStore(tmp_path)
    meta = await store.create_session()
    for i, text in enumerate(["first", "second"]):
        await store.append(meta.session_id, SessionNode(node_id=str(i),
            parent_id=str(i - 1) if i else None,
            message=Message(role="assistant", content=[Text(text=text)])))
    ctx = ToolCtx(extra={"session_view": store.session_slice})
    result = await session_view.handler({"session_id": meta.session_id, "text_offset": 2}, ctx)
    assert "[1 assistant] cond\n[end of message text]" in result.text
    empty = await session_view.handler({"session_id": meta.session_id, "text_offset": 6}, ctx)
    assert not empty.is_error
    for fields in [{"text_offset": -1}, {"text_offset": 7},
                   {"text_offset": 0, "count": 2}, {"text_offset": 0, "show": "tools"}]:
        result = await session_view.handler({"session_id": meta.session_id, **fields}, ctx)
        assert result.is_error

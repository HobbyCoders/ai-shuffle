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
        message=Message(role="assistant", content=[Text(text=text)]), ts=1_700_000_000.0))
    ctx = ToolCtx(extra={"session_view": store.session_slice,
                         "time_context": lambda _: {"zone": "America/New_York"}})
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
        heading, paged = result.text.split("\n[0 assistant] ", 1)
        assert "message 0 sent 2023-11-14T17:13:20-05:00" in heading
        body, footer = paged.rsplit("\n[", 1)
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


@pytest.mark.parametrize("ts,expected", [(1_700_000_000.0, "2023-11-14T22:13:20+00:00"),
                                         (0.0, "unknown time")])
def test_text_page_time_survives_a_clipped_title_without_entering_the_body(ts, expected):
    from types import SimpleNamespace
    from fluid.store.base import RecallMessage

    text = "  Tomorrow\n[keep this literal] 🦋  "
    window = SimpleNamespace(messages=[RecallMessage(role="user", text=text, ts=ts)],
                             start=7, total=8, session_id="example")
    result = session_view._text_page(window, 0, "x" * 2_000, "session time")
    heading, body = result.text.split("\n[7 user] ", 1)
    assert len(result.text) <= session_view._OUTPUT_CAP
    assert f"message 7 sent {expected}" in heading
    assert body == text + "\n[end of message text]"


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


async def test_a_stored_plan_note_is_harness_context_not_a_user_message(tmp_path):
    from fluid.messages.content import HARNESS_NOTE_META_KEY

    store = JsonlStore(tmp_path)
    meta = await store.create_session()
    rows = [("user", "do the work", None), ("assistant", "on it", None),
            ("user", "# Session plan: do the work", {HARNESS_NOTE_META_KEY: True}),
            ("assistant", "done", None)]
    for i, (role, text, flag) in enumerate(rows):
        await store.append(meta.session_id, SessionNode(node_id=str(i),
            parent_id=str(i - 1) if i else None,
            message=Message(role=role, content=[Text(text=text)], provider_meta=flag)))
    ctx = ToolCtx(extra={"session_view": store.session_slice})
    full = await session_view.handler({"session_id": meta.session_id}, ctx)
    assert "[2 harness note]" in full.text and "[2 user]" not in full.text
    talk = await session_view.handler({"session_id": meta.session_id, "show": "conversation"}, ctx)
    assert "Session plan" not in talk.text
    assert "[0 user]" in talk.text and "[3 assistant]" in talk.text

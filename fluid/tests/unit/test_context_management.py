"""Context management: cache-safe trimming, Note recitation, Amp-style handoff."""

from collections.abc import AsyncIterator

from fluid.context.compaction import CompactionConfig, Compactor
from fluid.context.trim import (
    USER_IMAGE_KEY, ToolResultTrimmer, TrimConfig, block_chars,
    estimate_tokens_of_messages,
)
from fluid.engine.native import NativeLoop
from fluid.events import Handoff, HandoffCreated, UserTurn
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.messages.content import Reasoning, Thinking
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.builtins.note import handler as note_handler
from fluid.tools.registry import ToolCtx


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _result_node(nid: str, tuid: str, chars: int, *, is_error: bool = False) -> SessionNode:
    return _node(nid, "user", [
        ToolResultBlock(tool_use_id=tuid, content=[Text("x" * chars)], is_error=is_error)
    ])


# ─── Trimming ────────────────────────────────────────────────────────────────


def _tool_result_texts(messages: list[Message]) -> list[str]:
    out = []
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                out.append("".join(getattr(c, "text", "") for c in b.content or []))
    return out


def test_trim_below_trigger_is_identity() -> None:
    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=10_000))
    nodes = [_result_node(f"n{i}", f"t{i}", 3_000) for i in range(3)]
    rendered = trimmer.apply(nodes)
    assert rendered == [n.message for n in nodes]  # untouched, same objects


def test_trim_batch_keeps_recent_and_errors() -> None:
    cfg = TrimConfig(trigger_tokens=10, keep_recent_tool_results=2,
                     min_result_chars=100)
    trimmer = ToolResultTrimmer(config=cfg)
    nodes = [
        _result_node("n0", "t0", 5_000),                  # old + big  → trimmed
        _result_node("n1", "t1", 50),                     # too small  → kept
        _result_node("n2", "t2", 5_000, is_error=True),   # error      → kept
        _result_node("n3", "t3", 5_000),                  # recent (K=2) → kept
        _result_node("n4", "t4", 5_000),                  # recent      → kept
    ]
    texts = _tool_result_texts(trimmer.apply(nodes))
    assert "trimmed to save context" in texts[0]
    assert texts[1] == "x" * 50
    assert texts[2] == "x" * 5_000  # error kept verbatim
    assert texts[3] == "x" * 5_000 and texts[4] == "x" * 5_000


def test_trim_is_sticky_and_batchy() -> None:
    """Once trimmed, always trimmed — and nothing NEW is trimmed while the
    rendered size sits under the trigger (prefix stays byte-identical)."""
    cfg = TrimConfig(trigger_tokens=3_000, keep_recent_tool_results=1,
                     min_result_chars=100)
    trimmer = ToolResultTrimmer(config=cfg)
    nodes = [_result_node(f"n{i}", f"t{i}", 8_000) for i in range(3)]
    first = _tool_result_texts(trimmer.apply(nodes))
    assert "trimmed" in first[0] and "trimmed" in first[1]
    assert first[2] == "x" * 8_000  # the most recent survived

    # Append one small result: n2 leaves the K-window but the rendered size is
    # now under trigger — n2 must NOT be newly trimmed (no per-turn cache burn).
    nodes.append(_result_node("n3", "t3", 200))
    second = _tool_result_texts(trimmer.apply(nodes))
    assert second[2] == "x" * 8_000
    assert second[:2] == first[:2]  # earlier trims unchanged (sticky)


def test_browser_screenshot_is_available_once_then_compacted() -> None:
    """A screenshot rides ONE request, then is compacted by the next.

    The estimator prices images at a flat ~1500 tokens (providers bill by
    pixels, and dispatch conditions images to a 1568px edge), so the historic
    base64-length blowup (a 531k PNG once made every Grok follow-up ~450k
    tokens) is structurally gone: the estimate stays small no matter how many
    bytes the image carries.
    """
    image_result = _node("image-result", "user", [
        ToolResultBlock(
            tool_use_id="shot-1",
            content=[
                Text("screenshot taken"),
                Image(media_type="image/png", data="a" * 531_476),
            ],
        )
    ])
    assistant = _node("assistant-after-image", "assistant", [Text("I inspected it")])
    # Armed: consumption needs pressure, so the trigger has to be reachable.
    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=1))

    first = trimmer.apply([image_result])
    assert any(
        isinstance(inner, Image)
        for block in first[0].content
        for inner in getattr(block, "content", [])
    )
    # ~1500 tokens for the image + the caption, regardless of base64 length.
    assert estimate_tokens_of_messages(first) < 5_000

    # A later assistant node proves the image was already delivered to one
    # model call, so with the trigger crossed it is consumed even though it is
    # the newest tool result.
    second = trimmer.apply([image_result, assistant])
    texts = _tool_result_texts(second)[0]
    assert "image consumed from context" in texts
    # The recovery route is the one that works: HistoryExpand returns text.
    assert "HistoryExpand cannot restore it" in texts
    # Only the image left; the caption beside it is still verbatim in context.
    assert "screenshot taken" in texts
    assert not any(
        isinstance(inner, Image)
        for block in second[0].content
        for inner in getattr(block, "content", [])
    )


def test_a_screenshot_survives_a_session_that_is_not_under_pressure() -> None:
    """The consume-once rule used to ignore the trigger entirely, so every
    screenshot rewrote the middle of an already-cached prefix at any context
    occupancy. Under the real trigger this transcript keeps its image."""
    image_result = _node("image-result", "user", [
        ToolResultBlock(
            tool_use_id="shot-1",
            content=[Image(media_type="image/png", data="a" * 531_476)],
        )
    ])
    assistant = _node("assistant-after-image", "assistant", [Text("seen")])
    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=10_000_000))

    rendered = trimmer.apply([image_result, assistant])
    assert any(
        isinstance(inner, Image)
        for block in rendered[0].content
        for inner in getattr(block, "content", [])
    )
    assert trimmer._image_consumed == set()


def test_consumed_screenshot_view_is_read_only_but_matches_next_request() -> None:
    image_result = _node("image-result", "user", [
        ToolResultBlock(
            tool_use_id="shot-1",
            content=[Image(media_type="image/png", data="a" * 20_000)],
        )
    ])
    assistant = _node("assistant-after-image", "assistant", [Text("done")])
    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=1))

    # The view reports the DECIDED state, so before apply() it still shows the
    # image — which is exactly what the next request would carry.
    assert "image consumed from context" not in _tool_result_texts(
        trimmer.view([image_result, assistant])
    )[0]
    assert trimmer._image_consumed == set()

    trimmer.apply([image_result, assistant])
    rendered = trimmer.view([image_result, assistant])
    assert "image consumed from context" in _tool_result_texts(rendered)[0]
    assert trimmer._trimmed == set()  # an image swap is not a full trim


def _screenshot_path() -> list[SessionNode]:
    """user → assistant(tool_use) → image result → assistant. The trailing
    assistant node is the evidence the image already reached one model call."""
    return [
        _node("u0", "user", [Text("look at this")]),
        _node("a0", "assistant", [
            Text("taking screenshot"),
            ToolUseBlock(id="call0", name="Screenshot", input={}),
        ]),
        _node("r0", "user", [
            ToolResultBlock(
                tool_use_id="call0",
                content=[Image(media_type="image/png", data="a" * 20_000)],
                is_error=False,
            )
        ]),
        _node("a1", "assistant", [Text("I see it")]),
    ]


def _has_image(message: Message) -> bool:
    return any(
        isinstance(inner, Image)
        for block in message.content
        for inner in getattr(block, "content", None) or []
    )


def test_compactor_trims_consumed_image_results() -> None:
    """Regression: Compactor._render_live rendered node-by-node, so the
    later-assistant evidence was invisible and consume-once never fired."""
    nodes = _screenshot_path()
    compactor = Compactor(config=CompactionConfig(
        trigger_tokens=1, trim=TrimConfig(trigger_tokens=1)
    ))

    assert _has_image(compactor.view(nodes)[2])  # nothing decided yet

    rendered, promotions = compactor.apply(nodes)
    assert not _has_image(rendered[2])
    assert "image consumed from context" in _tool_result_texts([rendered[2]])[0]
    # Nothing near the digest/epoch triggers, but the consume-once trim is
    # reported so an image leaving context is visible in the transcript.
    assert [(p.tier, p.reason) for p in promotions] == [(1, "image_consumed")]
    assert ("r0", "call0") in compactor.trimmer._image_consumed
    assert compactor.trimmer._trimmed == set()


def test_compactor_keeps_images_when_trim_disabled() -> None:
    nodes = _screenshot_path()
    compactor = Compactor(
        config=CompactionConfig(
            trigger_tokens=1, trim=TrimConfig(enabled=False, trigger_tokens=1)
        )
    )

    assert _has_image(compactor.view(nodes)[2])
    rendered, _promotions = compactor.apply(nodes)
    assert _has_image(rendered[2])
    assert compactor.trimmer._trimmed == set()


def test_compactor_keeps_unconsumed_image() -> None:
    """No later assistant node → the image has not been delivered yet."""
    nodes = _screenshot_path()[:-1]
    compactor = Compactor(config=CompactionConfig(
        trigger_tokens=1, trim=TrimConfig(trigger_tokens=1)
    ))
    rendered, promotions = compactor.apply(nodes)
    assert promotions == []
    assert _has_image(rendered[2])


# ─── Estimator: reasoning envelopes and user images ──────────────────────────


def test_reasoning_envelope_counts_what_is_resent() -> None:
    """A Reasoning block has no ``text``; it used to estimate as ~3 tokens
    while its 60 KB encrypted blob went on the wire every call."""
    blob = "e" * 60_000
    raw = {"type": "reasoning", "encrypted_content": blob, "summary": []}
    openai = Reasoning(
        origin="openai_responses", summary_text="short",
        encrypted_content=blob, raw=raw,
    )
    assert block_chars(openai) >= 60_000
    # raw already carries the blob; only one of them goes out.
    assert block_chars(openai) < 2 * 60_000
    # Legacy envelope without raw: the parts are what get sent.
    assert block_chars(
        Reasoning(origin="openai_responses", encrypted_content=blob)
    ) == 60_000
    thinking = "t" * 10_000
    anthropic = Reasoning(
        origin="anthropic", summary_text=thinking, signature="s" * 300,
        raw={"type": "thinking", "thinking": thinking, "signature": "s" * 300},
    )
    assert 10_300 <= block_chars(anthropic) < 2 * 10_300
    assert block_chars(Thinking(text="abc")) == 3
    # A message made of reasoning alone now moves the estimate.
    assert estimate_tokens_of_messages(
        [Message(role="assistant", content=[openai])]
    ) >= 15_000


def test_user_image_estimates_flat_and_is_consumed_after_one_call() -> None:
    """A top-level Image in a user turn: same flat weight and the same
    consume-once rule as a screenshot inside a tool result."""
    attachment = _node("u-img", "user", [
        Text("what is this?"),
        Image(media_type="image/png", data="a" * 400_000),
    ])
    assert 1_400 < estimate_tokens_of_messages([attachment.message]) < 1_700

    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=1))
    first = trimmer.apply([attachment])
    assert any(isinstance(b, Image) for b in first[0].content)  # first call sees it

    assistant = _node("a-after", "assistant", [Text("a cat")])
    second = trimmer.apply([attachment, assistant])
    assert not any(isinstance(b, Image) for b in second[0].content)
    text = "".join(getattr(b, "text", "") for b in second[0].content)
    assert "what is this?" in text  # the caption beside it survives
    assert "image attachment consumed from context" in text
    assert "attach it again" in text
    assert ("u-img", USER_IMAGE_KEY) in trimmer._image_consumed
    assert estimate_tokens_of_messages(second) < 300

    # The Compactor path (what the engine renders through) agrees, and the
    # decision is reported like any other consume-once trim.
    compactor = Compactor(config=CompactionConfig(
        trigger_tokens=1, trim=TrimConfig(trigger_tokens=1)
    ))
    rendered, promotions = compactor.apply([attachment, assistant])
    assert not any(isinstance(b, Image) for b in rendered[0].content)
    assert [(p.tier, p.reason) for p in promotions] == [(1, "image_consumed")]
    assert ("u-img", USER_IMAGE_KEY) in compactor.trimmer._image_consumed


# ─── Note builtin ────────────────────────────────────────────────────────────


async def test_note_add_recites_and_persists(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    first = await note_handler({"action": "add", "text": "step 1: build"}, ctx)
    second = await note_handler({"action": "add", "text": "step 2: ship"}, ctx)
    assert "step 1: build" in second.text and "step 2: ship" in second.text

    listing = await note_handler({"action": "list"}, ctx)
    assert listing.text == second.text  # recitation returns the full notes

    # Session-scoped: another session sees its own empty notes.
    other = await note_handler({"action": "list"}, ToolCtx(cwd=str(tmp_path), session_id="s2"))
    assert "(empty)" in other.text

    cleared = await note_handler({"action": "clear"}, ctx)
    assert "(empty)" in cleared.text


# ─── Handoff ─────────────────────────────────────────────────────────────────


class HandoffProvider:
    """Scripted: first call answers the turn, the handoff call returns a doc."""

    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        text = ("HANDOFF DOC: goal, state, next steps"
                if "handoff document" in request.messages[0].content[0].text
                else "worked on it")
        for chunk in [
            StreamChunk("message_start", {"usage": {"input_tokens": 1}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": text}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"}),
            StreamChunk("message_stop", {}),
        ]:
            yield chunk


async def test_handoff_creates_seeded_session_and_event() -> None:
    provider = HandoffProvider()
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)

    await loop.submit(UserTurn(content="do the thing"))
    await loop.wait_turn()
    await loop.submit(Handoff(goal="finish the migration"))

    events = [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    assert len(events) == 1
    ev = events[0]
    assert ev.goal == "finish the migration"
    assert "HANDOFF DOC" in ev.content

    # The new session exists and starts from the handoff document.
    new_path = await store.load_path(ev.new_session_id)
    assert len(new_path) == 1
    assert new_path[0].message.role == "user"
    assert "HANDOFF DOC" in new_path[0].message.text()

    # The summary request carried the transcript and the goal.
    handoff_req = provider.requests[-1]
    assert "finish the migration" in handoff_req.messages[0].content[0].text
    assert "do the thing" in handoff_req.messages[0].content[0].text


def test_trim_placeholder_keeps_a_head_excerpt() -> None:
    from fluid.context.trim import block_head, _placeholder
    from fluid.messages.content import Text, ToolResultBlock

    block = ToolResultBlock(
        tool_use_id="t", content=[Text(text="# Module 7 — secret: M7-1234\n" + "x" * 5000)]
    )
    head = block_head(block)
    assert head.startswith("# Module 7 — secret: M7-1234")
    text = _placeholder("node_abc", 5030, head).text
    assert "M7-1234" in text and "node_abc" in text and "do not reconstruct" in text

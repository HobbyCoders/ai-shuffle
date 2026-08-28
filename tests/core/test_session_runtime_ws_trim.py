"""WS-leg trimming of redundant render content (performance review W3).

The turn pump yields the COMPLETE assistant text block (``{"type": "text",
"content": ...}``, query_engine.py:1684) and a ``thinking_block`` marker after
the deltas that already carried the same characters. Neither had a dispatch case
in the frontend WS handler (``frontend/src/lib/stores/session/ws-message-handlers.ts``
— both fell through to ``default: break``), yet both were serialized, pushed to
the origin socket AND fanned out to every watching device, parsed, and dropped.
The same content already rides the SSE event feed as coalesced deltas.

Same class: ``tool_use`` carries the full tool input (a whole file body for
Write/Edit). The frontend's ``tool_use`` case reads only ``session_id`` and
``id`` (it maintains the pendingTools set); the rendered arguments come from the
SSE feed. So the FRAME is kept — it is correctness-bearing — but an oversized
input is replaced by a marker.

Nothing that a client needs for correctness is trimmed: every remaining frame
type (done / error / tool_use / tool_result / permission / question / session
lifecycle) still goes out in full.
"""

import json

import pytest

from app.core import session_runtime as sr


# ─── W3: text / thinking_block are trimmed off the WS leg ───────────────────


@pytest.mark.parametrize("etype", ["text", "thinking_block"])
def test_redundant_render_frames_are_trimmed(etype):
    assert etype in sr._WS_TRIMMED_RENDER_FRAMES
    assert sr._is_trimmed_render_frame(etype) is True


@pytest.mark.parametrize(
    "etype",
    [
        # Lifecycle / coordination the client cannot reconstruct from the feed.
        "done", "error", "interrupted", "start",
        "tool_use", "tool_result",
        "subagent_tool_use", "subagent_tool_result",
        "permission_request", "user_question",
        "session_created", "queued_message_processing",
    ],
)
def test_correctness_bearing_frames_are_not_trimmed(etype):
    assert sr._is_trimmed_render_frame(etype) is False


def test_previously_trimmed_frames_stay_trimmed():
    """No regression on the pre-existing trim set."""
    for etype in ("chunk", "stream_delta", "stream_block_start",
                  "stream_block_stop", "stream_message_delta",
                  "task_progress", "system", "usage_update"):
        assert sr._is_trimmed_render_frame(etype) is True
    # Unkept subagent render frames keep their prefix rule.
    assert sr._is_trimmed_render_frame("subagent_chunk") is True
    assert sr._is_trimmed_render_frame("subagent_tool_use") is False


def test_trim_does_not_disturb_model_output_detection():
    """`text` / `thinking_block` still mark model output for the steer-receipt
    logic — that check runs BEFORE the WS trim, on the raw stream."""
    assert "text" in sr._MODEL_OUTPUT_FRAMES
    assert "thinking_block" in sr._MODEL_OUTPUT_FRAMES


# ─── W3: oversized tool inputs are capped on the WS leg ─────────────────────


def test_small_tool_input_passes_through_untouched():
    event = {"type": "tool_use", "id": "t1", "input": {"path": "a.py"}}
    assert sr._cap_ws_tool_input(event) is event


def test_oversized_tool_input_is_capped_without_mutating_the_recorded_frame():
    big = "x" * (sr._WS_MAX_TOOL_INPUT_CHARS * 4)
    event = {"type": "tool_use", "id": "t1", "input": {"content": big}}

    capped = sr._cap_ws_tool_input(event)

    assert capped is not event
    # The recorder already holds `event`; it must be untouched.
    assert event["input"] == {"content": big}
    assert capped["input"]["_ws_truncated"] is True
    assert capped["input"]["_original_chars"] > sr._WS_MAX_TOOL_INPUT_CHARS
    assert len(capped["input"]["_preview"]) == sr._WS_MAX_TOOL_INPUT_CHARS
    # Everything the frontend actually reads survives.
    assert capped["id"] == "t1"
    assert capped["type"] == "tool_use"
    # And the capped frame is still serializable for the wire.
    json.loads(json.dumps(capped))


def test_subagent_tool_use_input_is_capped_too():
    big = "y" * (sr._WS_MAX_TOOL_INPUT_CHARS * 2)
    event = {"type": "subagent_tool_use", "id": "t2", "input": {"content": big}}
    assert sr._cap_ws_tool_input(event)["input"]["_ws_truncated"] is True


def test_non_tool_frames_are_never_capped():
    big = "z" * (sr._WS_MAX_TOOL_INPUT_CHARS * 2)
    for etype in ("tool_result", "done", "permission_request"):
        event = {"type": etype, "input": {"content": big}}
        assert sr._cap_ws_tool_input(event) is event


def test_unserializable_tool_input_still_caps_instead_of_raising():
    class _Weird:
        def __repr__(self):
            return "W" * (sr._WS_MAX_TOOL_INPUT_CHARS * 2)

    event = {"type": "tool_use", "id": "t3", "input": _Weird()}
    capped = sr._cap_ws_tool_input(event)
    assert capped is not event
    assert capped["input"]["_ws_truncated"] is True

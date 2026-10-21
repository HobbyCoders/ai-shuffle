"""Negotiated editor typography survives broker validation unchanged."""
import asyncio
import copy
import pytest
from app.core import browser_control as control


def editor_state():
    state = control.unavailable("runtime")
    state["editors"] = [dict(id="field", type="text", label="Name", value="Ada", x=1, y=2, width=200, height=30,
        style=dict(fontSize=18, lineHeight=None, paddingLeft=7, paddingTop=5, borderLeft=3, borderTop=4, fontFamily='"Example Font", sans-serif'))]
    return state


@pytest.mark.asyncio
async def test_editor_style_survives_report_unchanged(monkeypatch):
    monkeypatch.setattr(control, "_brokers", {})
    broker = control.get("typography")
    await broker.register("runtime")
    state = editor_state()
    expected = copy.deepcopy(state["editors"])
    try:
        await broker.report("runtime", state, [])
        assert broker.state["editors"] == expected
        # An older runtime may omit typography entirely.
        state["revision"] += 1
        del state["editors"][0]["style"]
        await broker.report("runtime", state, [])
        assert broker.state["editors"] == state["editors"]
    finally:
        broker.close()
        await asyncio.sleep(0)


@pytest.mark.parametrize("name", ["fontSize", "lineHeight", "paddingLeft", "paddingTop", "borderLeft", "borderTop"])
@pytest.mark.parametrize("value", [-1, 1001, float("nan"), float("inf"), True, "12px"])
def test_invalid_numeric_typography_is_rejected(name, value):
    state = editor_state()
    state["editors"][0]["style"][name] = value
    with pytest.raises(control.ControlError):
        control.validate_state(state, "runtime")


@pytest.mark.parametrize("value", [None, 123, "x" * 201])
def test_font_family_is_a_short_string(value):
    state = editor_state()
    state["editors"][0]["style"]["fontFamily"] = value
    with pytest.raises(control.ControlError):
        control.validate_state(state, "runtime")


def test_unknown_style_keys_are_rejected():
    state = editor_state()
    state["editors"][0]["style"]["cssText"] = "position:fixed"
    with pytest.raises(control.ControlError):
        control.validate_state(state, "runtime")

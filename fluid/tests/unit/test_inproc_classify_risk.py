"""An in-process tool's per-input risk reaches its ToolSpec, capped at ``risk``."""

from __future__ import annotations

from fluid.tools.inproc import InprocTool, create_inproc_server
from fluid.tools.registry import ToolRegistry


async def _handler(args: dict) -> dict:
    return {"content": [{"type": "text", "text": "ok"}]}


def _registry(classify=None) -> ToolRegistry:
    action = InprocTool(
        name="Device", description="drive a device",
        input_schema={"type": "object", "properties": {"action": {"type": "string"}}},
        handler=_handler, risk="write", classify_risk=classify,
    )
    reg = ToolRegistry()
    reg.register_inproc(create_inproc_server("srv", [action]))
    return reg


def test_classifier_lowers_the_risk_of_a_read_action() -> None:
    spec = _registry(lambda args: "safe" if args.get("action") == "status" else "write").get(
        "mcp__srv__Device")
    assert spec.risk == "write"
    assert spec.effective_risk({"action": "status"}) == "safe"
    assert spec.effective_risk({"action": "tap"}) == "write"


def test_classifier_cannot_raise_past_the_cap_and_a_broken_one_keeps_it() -> None:
    assert _registry(lambda args: "destructive").get("mcp__srv__Device").effective_risk({}) == "write"

    def broken(args: dict) -> str:
        raise RuntimeError("boom")

    assert _registry(broken).get("mcp__srv__Device").effective_risk({}) == "write"


def test_without_a_classifier_the_static_risk_applies() -> None:
    assert _registry().get("mcp__srv__Device").effective_risk({"action": "status"}) == "write"

"""Claude-Code-style permission modes on fluid's own engine.

``acceptEdits`` is ``on_write`` plus allow rules for the edit-class tools
(``fluid.permissions.modes.ACCEPT_EDITS_TOOLS``), so a file edit runs without
a prompt and everything else asks exactly as ``default`` does. The
``SetPermissionMode`` op installs those rules on the way in and lifts them on
the way out; configured rules keep outranking them.
"""

from __future__ import annotations

from fluid.engine.native import NativeLoop
from fluid.events import SetPermissionMode
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.modes import (
    ACCEPT_EDITS_TOOLS,
    MODE_POLICY,
    PERMISSION_MODES,
    accept_edits_rules,
    mode_rules,
)
from fluid.permissions.rules import Rule
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolSpec


def _spec(name: str, risk: str = "write") -> ToolSpec:
    return ToolSpec(name=name, description="", input_schema={}, risk=risk)


def _verdicts(engine: PermissionEngine) -> dict[str, str]:
    inp = {"file_path": "a.py", "command": "rm x", "code": "1", "url": "https://x"}
    return {
        name: engine.evaluate(_spec(name), inp)
        for name in (*sorted(ACCEPT_EDITS_TOOLS), "Bash", "run_python", "WebFetch", "Remember")
    }


def test_the_table() -> None:
    assert ACCEPT_EDITS_TOOLS == {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    assert PERMISSION_MODES == ("default", "acceptEdits", "bypassPermissions")
    assert MODE_POLICY["acceptEdits"] == MODE_POLICY["default"] == "on_write"
    assert MODE_POLICY["bypassPermissions"] == "never_ask"
    assert {r.tool for r in accept_edits_rules()} == ACCEPT_EDITS_TOOLS
    assert all(r.decision == "allow" and not r.input_globs for r in accept_edits_rules())
    assert mode_rules("default") == mode_rules("bypassPermissions") == mode_rules("on_write") == []
    assert [r.tool for r in mode_rules("acceptEdits")] == sorted(ACCEPT_EDITS_TOOLS)


def test_set_mode_rules_allows_edits_and_keeps_asking_for_the_rest() -> None:
    engine = PermissionEngine("on_write")
    engine.set_mode_rules(mode_rules("acceptEdits"))
    verdicts = _verdicts(engine)
    assert {n for n, v in verdicts.items() if v == "allow"} == ACCEPT_EDITS_TOOLS
    assert {n for n, v in verdicts.items() if v == "ask"} == set(verdicts) - ACCEPT_EDITS_TOOLS
    # Safe calls stay allowed by the policy, as under default.
    assert engine.evaluate(_spec("Read", "safe"), {"file_path": "a.py"}) == "allow"


def test_set_mode_rules_lifts_only_the_rules_it_installed() -> None:
    configured_allow = Rule(tool="Write", decision="allow")  # looks like a mode rule
    configured_deny = Rule(tool="Edit", decision="deny", input_globs={"file_path": "*.lock"})
    engine = PermissionEngine("on_write", rules=[configured_allow, configured_deny])

    engine.set_mode_rules(mode_rules("acceptEdits"))
    # Mode rules go in front: the configured deny still wins for its files.
    assert engine.rules[: len(ACCEPT_EDITS_TOOLS)] == mode_rules("acceptEdits")
    assert engine.evaluate(_spec("Edit"), {"file_path": "x.lock"}) == "deny"
    assert engine.evaluate(_spec("Edit"), {"file_path": "a.py"}) == "allow"

    engine.set_mode_rules(mode_rules("default"))
    assert engine.rules == [configured_allow, configured_deny]
    assert engine.evaluate(_spec("Edit"), {"file_path": "a.py"}) == "ask"
    assert engine.evaluate(_spec("Write"), {"file_path": "a.py"}) == "allow"  # the configured one

    # Installing twice never doubles the rules.
    engine.set_mode_rules(mode_rules("acceptEdits"))
    engine.set_mode_rules(mode_rules("acceptEdits"))
    assert len(engine.rules) == len(ACCEPT_EDITS_TOOLS) + 2


async def test_set_permission_mode_op_swaps_policy_and_rules() -> None:
    perms = PermissionEngine("on_write")
    loop = NativeLoop(provider=None, store=MemoryStore(), model="m", permissions=perms)

    await loop.submit(SetPermissionMode(mode="acceptEdits"))
    assert perms.approval_policy == "on_write"
    verdicts = _verdicts(perms)
    assert {n for n, v in verdicts.items() if v == "allow"} == ACCEPT_EDITS_TOOLS
    assert verdicts["Bash"] == verdicts["run_python"] == verdicts["WebFetch"] == "ask"

    await loop.submit(SetPermissionMode(mode="default"))
    assert perms.approval_policy == "on_write"
    assert set(_verdicts(perms).values()) == {"ask"}

    await loop.submit(SetPermissionMode(mode="bypassPermissions"))
    assert perms.approval_policy == "never_ask"
    assert set(_verdicts(perms).values()) == {"allow"}
    assert perms.rules == []

    # A raw policy name clears the mode's rules too.
    await loop.submit(SetPermissionMode(mode="acceptEdits"))
    await loop.submit(SetPermissionMode(mode="always_ask"))
    assert perms.approval_policy == "always_ask"
    assert perms.rules == []

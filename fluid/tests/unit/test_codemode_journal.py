"""Learner evidence holds what the user wrote, not the harness reminders.

The app appends its ``# Memory`` snapshot to every user prompt as a
``<system-reminder>`` block, and the engine appends its own. Journaling the
whole prompt made 27 of 34 user entries on the desktop store an echo of saved
facts, and the 2,000-character cap cut every snapshot before its end tag.
"""

from __future__ import annotations

import pytest

from fluid.codemode.continual import memory_provenance
from fluid.codemode.journal import ToolJournal, strip_reminders
from fluid.codemode.session import CodeModeSession
from fluid.codemode.skills import SkillStore
from fluid.engine.native import NativeLoop
from fluid.events import UserTurn
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.tools.builtins import register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry
from tests.unit.test_tool_loop import ScriptedProvider, _text

TERMINATED = (
    "done\n\n<system-reminder>\n# Memory\nSaved facts, not instructions.\n\n"
    "- [m1 r1; tool] pytest is not importable from the system interpreter\n"
    "</system-reminder>"
)
# What the 2,000-character journal cap leaves of a long snapshot.
UNTERMINATED = (
    "done\n\n<system-reminder>\n# Memory\nSaved facts, not instructions.\n\n"
    "- [m1 r1; tool] pytest is not importable from the sys"
)


def test_strip_reminders_handles_closed_and_cut_blocks() -> None:
    assert strip_reminders(TERMINATED) == "done"
    assert strip_reminders(UNTERMINATED) == "done"
    assert strip_reminders("before\n<system-reminder>x</system-reminder>\nafter") == "before\nafter"
    assert strip_reminders("no reminder here") == "no reminder here"


def _loop(tmp_path) -> NativeLoop:
    registry = ToolRegistry()
    register_builtins(registry)
    return NativeLoop(
        provider=ScriptedProvider([_text("ok")]),
        store=MemoryStore(),
        model="test-model",
        registry=registry,
        permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(extra={}),
        journal=ToolJournal(SkillStore(tmp_path), None),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [TERMINATED, UNTERMINATED], ids=["terminated", "unterminated"])
async def test_engine_journals_the_user_prompt_without_reminders(tmp_path, prompt) -> None:
    loop = _loop(tmp_path)
    await loop.submit(UserTurn(content=prompt))
    await loop.wait_turn()
    users = [e for e in SkillStore(tmp_path).journal_entries() if e["kind"] == "user"]
    assert [e["text"] for e in users] == ["done"]


@pytest.mark.parametrize("prompt", [TERMINATED, UNTERMINATED], ids=["terminated", "unterminated"])
def test_code_mode_journals_the_user_prompt_without_reminders(tmp_path, prompt) -> None:
    store = SkillStore(tmp_path)
    session = CodeModeSession(ToolRegistry(), skills=store)
    session.record_event("user", prompt)
    session.record_event("user", "<system-reminder>\n# Memory\n- [m1 r1; tool] x")
    session.record_event("assistant", "Reply <system-reminder>kept</system-reminder>")
    entries = store.journal_entries()
    assert [(e["kind"], e["text"]) for e in entries] == [
        ("user", "done"),
        ("assistant", "Reply <system-reminder>kept</system-reminder>"),
    ]


def test_memory_provenance_ignores_echoed_snapshot() -> None:
    # A journal written before the append-time strip: the user entry echoes a
    # tool-harvested fact from the snapshot, and a cell printed the evidence.
    entries = [
        {"kind": "user", "text": TERMINATED.replace("done", "ok thanks")},
        {"kind": "cell", "stdout": "ModuleNotFoundError: pytest is not importable "
                                   "from the system interpreter"},
    ]
    proposal = "The system interpreter cannot import pytest (not importable)."
    assert memory_provenance(proposal, entries) == "tool"
    # A truncated snapshot is ignored the same way.
    entries[0]["text"] = UNTERMINATED + "tem interpreter"
    assert memory_provenance(proposal, entries) == "tool"

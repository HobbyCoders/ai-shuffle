"""ToolJournal — the continual harness's episodic recorder for JSON-mode.

Code-mode sessions journal through :class:`CodeModeSession` (executed cells +
conversation events). Before this module, a plain JSON-tool session journaled
nothing and therefore learned nothing — the learning loop only existed behind
``code_mode = true``. The ToolJournal closes that gap: it records the same
episodic shape (user prompts, final assistant replies) plus one entry per tool
call, into the same ``journal.jsonl`` the synthesizer reads, and kicks the
same background synthesis pass at turn boundaries.

Synthesized skills are Python modules for the code-mode kernel — a JSON-mode
session cannot *import* them, but its journals still teach the library
(recurring workflows, corrections, deletions), and everything learned is
available the moment code mode is on. Failure posture matches the rest of the
continual harness: journaling is best-effort and must never break a turn.
"""

from __future__ import annotations

import json
from typing import Any

from fluid.codemode.continual import Synthesizer
from fluid.codemode.skills import SkillStore

_EVENT_CAP = 1_500
_INPUT_CAP = 600
_OUTPUT_CAP = 400


class ToolJournal:
    def __init__(
        self, skills: SkillStore, synthesizer: Synthesizer | None = None
    ) -> None:
        self._skills = skills
        self._synthesizer = synthesizer

    def record_event(self, kind: str, text: str) -> None:
        """Journal a conversation event (``user`` prompt / ``assistant`` reply)."""
        if not (text or "").strip():
            return
        try:
            self._skills.journal_append({"kind": kind, "text": text[:_EVENT_CAP]})
        except Exception:
            pass
        if kind == "assistant" and self._synthesizer is not None:
            try:
                self._synthesizer.kick()
            except Exception:
                pass

    def record_tool(
        self, name: str, input: dict[str, Any], output: str, is_error: bool
    ) -> None:
        """Journal one JSON-surface tool call (compact previews only)."""
        try:
            compact = json.dumps(input, ensure_ascii=False, default=str)
        except Exception:
            compact = str(input)
        entry: dict[str, Any] = {
            "kind": "tool",
            "name": name,
            "input": compact[:_INPUT_CAP],
            "output": " ".join((output or "").split())[:_OUTPUT_CAP],
        }
        if is_error:
            entry["error"] = True
        try:
            self._skills.journal_append(entry)
        except Exception:
            pass

    async def aclose(self) -> None:
        if self._synthesizer is not None:
            try:
                await self._synthesizer.drain()
            except Exception:
                pass

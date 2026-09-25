"""ToolJournal + FrictionDetector — the continual harness's episodic recorders.

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

:class:`FrictionDetector` is the second recorder here, shared by both surfaces
(``ToolJournal`` for JSON mode, ``CodeModeSession`` for in-kernel calls).
Models rarely self-report an expensive harness mistake, so the harness
detects it MECHANICALLY: a
not-found on a path the model constructed, a tool failing over and over, a
re-read of content already delivered, a no-match search re-run with
near-identical arguments. Each observation lands as a ``friction`` journal
entry carrying the mechanical facts and no interpretation — deciding what the
facts MEAN is the synthesizer's job, not the detector's. Friction does NOT
make the next synthesis pass urgent; it waits for the normal slice like any
other entry.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from fluid.codemode.continual import Synthesizer
from fluid.codemode.skills import SkillStore

_EVENT_CAP = 1_500
_INPUT_CAP = 600
_OUTPUT_CAP = 400

# A <system-reminder> block, closed or cut off before its end tag. The app
# appends its "# Memory" snapshot to the user prompt and the engine adds its
# own reminders; stripping them keeps journaled user entries from echoing saved
# facts.
_REMINDER_RE = re.compile(r"\n*<system-reminder>.*?(?:</system-reminder>|\Z)", re.S)


def strip_reminders(text: str) -> str:
    """``text`` without harness reminder blocks, for learner evidence.

    An unterminated block runs to the end of the text: that is what a
    reminder looks like after a length cap cuts it.
    """
    return _REMINDER_RE.sub("", text or "").strip()


# ── friction detection ──────────────────────────────────────────────────────

#: Journal ``kind`` for a mechanically-detected harness-usage mistake.
FRICTION_KIND = "friction"

#: A pathological turn must not flood the journal — the synthesizer reads a
#: bounded slice, and 40 copies of the same failure would crowd out the rest.
MAX_FRICTION_PER_TURN = 5
#: Consecutive failed calls to ONE tool before the streak counts as friction.
FAIL_STREAK_N = 3

_FRICTION_TEXT_CAP = 300
_FRICTION_ARGS_CAP = 200
#: Argument-similarity threshold for "retried with near-identical arguments".
NEAR_IDENTICAL_RATIO = 0.9

# Not-found diagnostics emitted by the file/search builtins (read.py, edit.py,
# glob.py, grep.py) plus the raw OS phrasing that reaches Bash and MCP tools.
# Matched case-insensitively against an error result only.
_NOT_FOUND_MARKERS = (
    "file not found:",
    "path not found:",
    "path does not exist:",
    "directory not found:",
    "notebook not found:",
    "no such file or directory",
)

# Verbatim heads of the repeat-work notes in fluid/tools/builtins/_util.py
# (``unchanged_read_note`` / ``repeat_search_pointer``). The repeat-work
# registries on ToolCtx already did the detection; this hooks their signal
# rather than rebuilding it. tests/unit/test_friction.py pins these against
# the real note text so a rewording there cannot silently unhook us.
_REPEAT_MARKERS = (
    "[note: unchanged since your earlier read this session",
    "[identical search already run this session",
)

# Result bodies that mean "this call found nothing" without being an error.
_EMPTY_MARKERS = ("(no matches)",)


def _clip(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[:cap] + "…"


def _compact_args(input: Any) -> str:
    try:
        compact = json.dumps(input, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        compact = str(input)
    return _clip(compact, _FRICTION_ARGS_CAP)


def _looks_empty(text: str) -> bool:
    """Did this (non-error) result find nothing?"""
    if not text:
        return True
    return any(marker in text for marker in _EMPTY_MARKERS)


def _near_identical(a: str, b: str) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= NEAR_IDENTICAL_RATIO


class FrictionDetector:
    """Per-turn, mechanical detection of harness-usage mistakes.

    Fed one ``(name, input, output, is_error)`` tuple per completed tool call —
    from the JSON dispatch seam or from the code-mode host-call bridge — and
    returns the journal entries that call earned (usually none). Four signals:

    ``path_not_found``
        The call failed with a not-found diagnostic about a path. The model
        constructed that path; the harness knows it was wrong.
    ``error_streak``
        ``fail_streak`` consecutive failed calls to the SAME tool in one turn
        (a success by that tool resets its counter).
    ``repeat_work``
        The result carried one of the harness's own repeat-work notes: an
        unchanged re-read, or a search already run verbatim this session.
    ``repeat_after_empty``
        A call that found nothing, immediately followed by a call to the same
        tool with near-identical arguments.

    Everything is best-effort: :meth:`observe` swallows its own errors, because
    a detector bug must never take down a turn. Nothing here interprets — the
    entries state what happened, and the background synthesizer decides whether
    any of it generalizes.
    """

    def __init__(
        self,
        *,
        max_per_turn: int = MAX_FRICTION_PER_TURN,
        fail_streak: int = FAIL_STREAK_N,
    ) -> None:
        self._max = max(0, max_per_turn)
        self._streak_n = max(2, fail_streak)
        self._recorded = 0
        self._fails: dict[str, int] = {}
        self._empty: dict[str, str] = {}
        self.begin_turn()

    def begin_turn(self) -> None:
        """Reset the per-turn budget and all cross-call state."""
        self._recorded = 0
        self._fails = {}
        self._empty = {}

    @property
    def count(self) -> int:
        """Friction entries recorded so far this turn."""
        return self._recorded

    def observe(
        self, name: str, input: Any, output: str, is_error: bool
    ) -> list[dict[str, Any]]:
        """Journal entries earned by one completed tool call (often empty)."""
        try:
            return self._observe(name, input, output, is_error)
        except Exception:
            return []

    # ── internals ────────────────────────────────────────────────────────
    def _observe(
        self, name: str, input: Any, output: str, is_error: bool
    ) -> list[dict[str, Any]]:
        name = str(name or "?")
        text = " ".join((output or "").split())
        args = _compact_args(input)
        found: list[dict[str, Any]] = []

        # 4. Empty result retried with near-identical arguments. Evaluated
        #    BEFORE this call updates the "last empty" slot, so the pair is
        #    (previous call, this call).
        previous = self._empty.get(name)
        if previous is not None and _near_identical(previous, args):
            found.append(self._entry(
                name, "repeat_after_empty",
                f"{name} found nothing, then was called again with "
                f"near-identical arguments: {args}",
            ))
        if not is_error and _looks_empty(text):
            self._empty[name] = args
        else:
            self._empty.pop(name, None)

        # 3. The harness's own repeat-work notes (hooked, not re-derived).
        if any(marker in text for marker in _REPEAT_MARKERS):
            found.append(self._entry(
                name, "repeat_work",
                f"{name}({args}) returned content already delivered this "
                f"session; the harness noted it as repeat work",
            ))

        # 1. Not-found on a path the model constructed.
        lowered = text.lower()
        if is_error and any(m in lowered for m in _NOT_FOUND_MARKERS):
            found.append(self._entry(
                name, "path_not_found", f"{name}({args}) failed: {text}",
            ))

        # 2. N consecutive failures of one tool within the turn.
        if is_error:
            streak = self._fails.get(name, 0) + 1
            self._fails[name] = streak
            if streak == self._streak_n:
                found.append(self._entry(
                    name, "error_streak",
                    f"{name} failed {streak} times in a row this turn; "
                    f"last error: {text}",
                ))
        else:
            self._fails.pop(name, None)

        room = max(0, self._max - self._recorded)
        found = found[:room]
        self._recorded += len(found)
        return found

    def _entry(self, name: str, signal: str, text: str) -> dict[str, Any]:
        return {
            "kind": FRICTION_KIND,
            "signal": signal,
            "name": name,
            "text": _clip(text, _FRICTION_TEXT_CAP),
        }


def append_friction(
    skills: SkillStore | None,
    synthesizer: Any | None,
    entries: list[dict[str, Any]],
) -> None:
    """Write friction entries to the journal. Never raises.

    Deliberately neither ``mark_urgent()`` nor ``kick()``: friction waits for
    the normal ``MIN_NEW_ENTRIES`` slice (a single event is noise), and a
    mid-turn pass would race the rest of the turn's evidence.
    """
    if skills is None or not entries:
        return
    for entry in entries:
        try:
            skills.journal_append(entry)
        except Exception:
            pass


def record_friction(
    skills: SkillStore | None,
    synthesizer: Any | None,
    detector: FrictionDetector,
    name: str,
    input: Any,
    output: str,
    is_error: bool,
) -> list[dict[str, Any]]:
    """Observe one tool call and journal whatever friction it earned."""
    entries = detector.observe(name, input, output, is_error)
    append_friction(skills, synthesizer, entries)
    return entries


class ToolJournal:
    def __init__(
        self,
        skills: SkillStore,
        synthesizer: Synthesizer | None = None,
        *,
        friction: FrictionDetector | None = None,
    ) -> None:
        self._skills = skills
        self._synthesizer = synthesizer
        self._friction = friction if friction is not None else FrictionDetector()

    @property
    def friction_count(self) -> int:
        """Friction entries recorded this turn (the engine's nudge trigger)."""
        return self._friction.count

    def begin_turn(self) -> None:
        """Turn boundary: reset friction budget and cross-call state."""
        try:
            self._friction.begin_turn()
        except Exception:
            pass

    def record_event(self, kind: str, text: str) -> None:
        """Journal a conversation event (``user`` prompt / ``assistant`` reply)."""
        if kind == "user":
            text = strip_reminders(text)
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
        record_friction(
            self._skills, self._synthesizer, self._friction,
            name, input, output, is_error,
        )

    async def aclose(self) -> None:
        if self._synthesizer is not None:
            try:
                await self._synthesizer.drain()
            except Exception:
                pass

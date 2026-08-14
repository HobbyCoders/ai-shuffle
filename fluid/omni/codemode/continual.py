"""Continual synthesis — distill the exec journal into persistent skills.

The "continual harness" loop: every code-mode cell is journaled by the
:class:`~omni.codemode.skills.SkillStore`; once enough new entries accumulate
(or at session close) the :class:`Synthesizer` makes one plain completion call
— same provider/model the session runs on, no tools — showing the model its
existing skill library plus the new journal slice, and asking it to distill
*reusable* workflow improvements into skill modules. Parsed skills are
validated and saved through the store, so they mount into the kernel's
``skills`` package on the next cell and in every future session.

Failure posture: synthesis is best-effort and must never break a turn. The
trigger runs as a single-flight background task; any provider or parse error
is recorded on ``last_error`` and swallowed. The journal cursor only advances
after a successful pass, so failed slices are retried next time.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from omni.codemode.skills import SkillStore, validate_skill
from omni.messages.content import Message, Text
from omni.providers.base import ProviderRequest

_SYNTH_SYSTEM = """\
You maintain a library of reusable Python "skills" for a code-mode agent
harness. Skills are small modules mounted into the agent's Python kernel as
`skills.<name>`; the agent imports them instead of re-deriving the same
helper code session after session.

You will see the current skill library and a journal of recently executed
cells. Distill only *genuinely reusable* workflow improvements:
- recurring multi-step patterns worth naming (not one-off task logic);
- generalize: parameters instead of hard-coded values from the journal;
- prefer updating/merging an existing skill over adding a near-duplicate;
- delete skills that are wrong or superseded;
- most journals need NO new skill — that is the correct common outcome.

Skill module rules: module docstring required (first line = one-sentence
description); define a function named exactly like the skill; only the
kernel's stdlib allowlist plus `tools.*` / other `skills.*` imports are
available; no filesystem or network access.

Output format — nothing else:
- for each new/updated skill:
  <skill name="snake_case_name">
  ...full module source...
  </skill>
- to remove a skill: <delete-skill name="snake_case_name"/>
- if nothing is worth keeping: NO_CHANGES
"""

_SKILL_RE = re.compile(r'<skill name="([a-z0-9_]+)">\n?(.*?)</skill>', re.DOTALL)
_DELETE_RE = re.compile(r'<delete-skill name="([a-z0-9_]+)"\s*/>')

# Journal previews are already trimmed at write time; cap the slice we show.
MAX_ENTRIES_PER_PASS = 40
MIN_NEW_ENTRIES = 8


class Synthesizer:
    def __init__(
        self,
        provider: Any,
        model: str,
        store: SkillStore,
        *,
        min_new_entries: int = MIN_NEW_ENTRIES,
        max_entries: int = MAX_ENTRIES_PER_PASS,
        max_tokens: int = 4096,
    ) -> None:
        self._provider = provider
        self._model = model
        self._store = store
        self._min_new = max(1, min_new_entries)
        self._max_entries = max_entries
        self._max_tokens = max_tokens
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    # ─── triggering ──────────────────────────────────────────────────────
    def due(self) -> bool:
        return self._store.journal_count() - self._store.synth_cursor() >= self._min_new

    def kick(self) -> None:
        """Start a background pass if one is due and none is running."""
        if self._task is not None and not self._task.done():
            return
        if not self.due():
            return
        self._task = asyncio.get_running_loop().create_task(self._run_safe())

    async def drain(self) -> None:
        """Await any in-flight pass, then run a final one if entries remain.

        Called from session close; only synthesizes when the remaining slice
        meets the normal threshold, so short sessions don't pay a model call.
        """
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        if self.due():
            await self._run_safe()

    async def _run_safe(self) -> list[str]:
        try:
            return await self.run()
        except Exception as exc:  # never let synthesis break the session
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []

    # ─── one synthesis pass ──────────────────────────────────────────────
    async def run(self) -> list[str]:
        """One pass over the un-synthesized journal slice. Returns saved names."""
        cursor = self._store.synth_cursor()
        entries = self._store.journal_entries(cursor, self._max_entries)
        if not entries:
            return []
        text = await self._complete(self._prompt(entries))
        changed = self._apply(text)  # sets last_error (None on a clean pass)
        self._store.set_synth_cursor(cursor + len(entries))
        return changed

    def _prompt(self, entries: list[dict]) -> str:
        lines = ["## Current skill library"]
        skills = self._store.list()
        if not skills:
            lines.append("(empty)")
        for s in skills:
            lines.append(f"### skills.{s.name}  (used {s.uses}x)")
            lines.append("```python")
            lines.append(s.source.rstrip("\n"))
            lines.append("```")
        lines.append("")
        lines.append("## Journal of recently executed cells")
        for i, e in enumerate(entries, 1):
            lines.append(f"### cell {i}")
            lines.append("```python")
            lines.append(str(e.get("code", "")).rstrip("\n"))
            lines.append("```")
            summary = {
                k: e[k]
                for k in ("stdout", "value", "error", "host_calls", "tools")
                if e.get(k)
            }
            if summary:
                lines.append(f"outcome: {json.dumps(summary, ensure_ascii=False)}")
        return "\n".join(lines)

    async def _complete(self, prompt: str) -> str:
        request = ProviderRequest(
            model=self._model,
            messages=[Message(role="user", content=[Text(text=prompt)])],
            system=_SYNTH_SYSTEM,
            tools=[],
            max_tokens=self._max_tokens,
        )
        parts: list[str] = []
        async for chunk in self._provider.stream(request):
            if chunk.kind == "text_delta":
                parts.append(chunk.payload["text"])
        return "".join(parts)

    def _apply(self, text: str) -> list[str]:
        """Parse the model's output and apply valid changes. Returns names saved
        or deleted; invalid skills are skipped (recorded on ``last_error``)."""
        changed: list[str] = []
        errors: list[str] = []
        for match in _SKILL_RE.finditer(text or ""):
            name, source = match.group(1), match.group(2).strip("\n") + "\n"
            error = validate_skill(name, source)
            if error is not None:
                errors.append(f"{name}: {error}")
                continue
            self._store.save(name, source)
            changed.append(name)
        for match in _DELETE_RE.finditer(text or ""):
            if self._store.delete(match.group(1)):
                changed.append(f"-{match.group(1)}")
        self.last_error = "; ".join(errors) if errors else None
        return changed

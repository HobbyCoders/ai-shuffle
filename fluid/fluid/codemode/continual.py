"""Continual synthesis — distill the exec journal into persistent skills.

The "continual harness" loop: every code-mode cell — and every conversation
turn (user prompt / assistant reply, the episodic half) — is journaled by the
:class:`~fluid.codemode.skills.SkillStore`; once enough new entries accumulate
(or at session close) the :class:`Synthesizer` makes one plain completion call
— no tools; the session's provider, or a dedicated ``continual_model`` —
showing the model its existing skill library plus the new journal slice, and
asking it to distill *reusable* workflow improvements into skill modules.
Parsed candidates are statically validated **and verified** (import +
``_selftest`` in a throwaway no-tools kernel, see ``verify.py``) before they
are saved; only proven skills mount into the kernel's ``skills`` package on
the next cell and in every future session.

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

from fluid.codemode.skills import SkillStore, validate_skill
from fluid.codemode.verify import verify_sources
from fluid.messages.content import Message, Text
from fluid.providers.base import ProviderRequest

_SYNTH_SYSTEM = """\
You maintain a library of reusable Python "skills" for a code-mode agent
harness. Skills are small modules mounted into the agent's Python kernel as
`skills.<name>`; the agent imports them instead of re-deriving the same
helper code session after session.

You will see the current skill library and a journal of the recent session:
conversation events ([user] prompts and [assistant] replies — these carry the
intent behind the work and whether the user accepted it) interleaved with the
cells that were executed. Distill only *genuinely reusable* workflow
improvements:
- recurring multi-step patterns worth naming (not one-off task logic);
- generalize: parameters instead of hard-coded values from the journal;
- prefer updating/merging an existing skill over adding a near-duplicate;
- delete skills that are wrong or superseded;
- weight user feedback: a workflow the user corrected or rejected is a signal
  to fix or delete, not to enshrine;
- most journals need NO new skill — that is the correct common outcome.

Skill module rules: module docstring required (first line = one-sentence
description); define a function named exactly like the skill; include a pure
`_selftest()` that exercises the skill on inline fixture data and raises on
failure — every skill is verified (import + _selftest, no tools available)
before it is accepted, and a skill without a self-test cannot prove it works;
only the kernel's stdlib allowlist plus `tools.*` / other `skills.*` imports
are available; no filesystem or network access.

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
        changed = await self._apply(text)  # sets last_error (None on a clean pass)
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
        lines.append("## Session journal (conversation + executed cells, in order)")
        cell_no = 0
        for e in entries:
            kind = e.get("kind", "cell")
            if kind in ("user", "assistant"):
                lines.append(f"[{kind}] {str(e.get('text', '')).strip()}")
                continue
            cell_no += 1
            lines.append(f"### cell {cell_no}")
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

    async def _apply(self, text: str) -> list[str]:
        """Parse the model's output, verify candidates, apply what passes.

        Candidates that fail static validation OR the verification kernel
        (import + ``_selftest``) are skipped and recorded on ``last_error`` —
        synthesized knowledge has to prove it works before it lands. Returns
        the names saved (or ``-name`` for deletions).
        """
        changed: list[str] = []
        errors: list[str] = []
        candidates: dict[str, str] = {}
        for match in _SKILL_RE.finditer(text or ""):
            name, source = match.group(1), match.group(2).strip("\n") + "\n"
            error = validate_skill(name, source)
            if error is not None:
                errors.append(f"{name}: {error}")
                continue
            candidates[name] = source
        if candidates:
            try:
                verdicts = await verify_sources(
                    {**self._store.sources(), **candidates}, list(candidates)
                )
            except Exception as exc:
                # Verification infrastructure failure: reject the batch rather
                # than persist unproven skills.
                verdicts = {n: f"verification unavailable: {exc}" for n in candidates}
            for name, source in candidates.items():
                verdict = verdicts.get(name)
                if verdict is not None:
                    errors.append(f"{name}: verification failed: {verdict}")
                    continue
                self._store.save(name, source)
                self._store.mark_verified(name)
                changed.append(name)
        for match in _DELETE_RE.finditer(text or ""):
            if self._store.delete(match.group(1)):
                changed.append(f"-{match.group(1)}")
        self.last_error = "; ".join(errors) if errors else None
        return changed

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
import time
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

You also maintain the agent's remembered FACTS — a short declarative memory
list shown to the agent at the start of every future session. Skills capture
HOW (procedures); memories capture WHAT IS TRUE (preferences, conventions,
corrections).

You will see the current skill library, the current memory list, and a journal
of the recent session: conversation events ([user] prompts and [assistant]
replies — these carry the intent behind the work and whether the user accepted
it) interleaved with the cells that were executed. Distill only *genuinely
reusable* workflow improvements:
- recurring multi-step patterns worth naming (not one-off task logic);
- generalize: parameters instead of hard-coded values from the journal;
- prefer updating/merging an existing skill over adding a near-duplicate;
- delete skills that are wrong or superseded;
- weight user feedback: a workflow the user corrected or rejected is a signal
  to fix or delete, not to enshrine;
- most journals need NO new skill — that is the correct common outcome.

Memory rules — add a memory for a durable declarative fact: a preference the
user stated, a convention of their environment, a correction that should never
need repeating, or anything the user explicitly asked to remember ("remember
that…" is a direct order to add one). A journal entry marked [remember] is the
agent itself flagging a fact as durable, usually on the user's direct request
— honor it: distill it into a clean memory entry (or fold it into an existing
one) unless it is transient task state or a secret. Rewrite a memory the
evidence shows is wrong or stale (emit it with its id); delete one the user
contradicted or that no longer applies. A memory is one or two sentences of
fact ("user prefers X", "staging deploys go through Y") — never task progress,
never instructions to yourself ("always do X" replays as a directive and
causes repeated work), and never secrets. Scope: a fact about THIS project's
codebase, layout, or conventions gets scope="project"; a fact about the user
or their machine in general stays global (the default). Most journals need NO
memory change either — but a [remember] entry almost always needs one.
Channel rule: a recurring mechanical failure of the MODEL itself — narrating
an action instead of performing it, repeatedly malformed calls, drifting from
a stated format — is not fixed by a memory (a memory tells the next session
what is true; it cannot change how the model behaves). That evidence belongs
in a model-guidance proposal (below), optionally alongside a memory about the
user's preference.

Model guidance proposals — the harness keeps a per-model-family guidance file
(shown to you when one exists) that steers HOW a given model family behaves in
the harness. When the journal shows a RECURRING mechanical failure specific to
this model — describing an intended action instead of calling the tool,
repeatedly malformed calls of the same shape, drifting from a stated output
convention, stopping after a stub — you may propose an edit. This is rare:
most journals need none, and a one-off mistake never qualifies. But when the
user rejects the SAME mechanical behavior more than once in a journal, that
is exactly the recurring evidence this channel exists for — propose the edit
rather than only noting the preference as a memory. Proposals are
NOT applied; they are queued and committed only if they survive a replay gate
against regression anchors, so every proposal must carry a motivation: a
natural task prompt that reproduces the failure (never naming a tool) and
checks that detect the fixed behavior. The motivation replays in a fresh,
EMPTY workspace — include a "setup" object of seed files whenever the task
needs content to act on. Write checks that detect the fix robustly: assert
the flow fired ({"kind": "tool_called", ...}) or forbid the failure's own
phrasing ({"kind": "not_contains", ...}); never guess at reply vocabulary —
a wording-specific "contains" fails even when the behavior is fixed. Propose
the FULL replacement file (short imperative lines), with stem = a lowercase
substring of the session model id.

Do NOT capture (these become persistent self-imposed constraints that bite
later, when the environment has changed):
- environment-dependent failures (missing binaries, install errors) — the
  user can fix those; they are not durable rules;
- negative claims about tools or capabilities ("X tool is broken", "Y does
  not work") — these harden into standing refusals cited long after the
  problem was fixed;
- transient errors that resolved within the session — if retrying worked,
  the lesson is the retry pattern, not the failure;
- unresolved failures — a session that never found a working method must not
  be written up as a validated workflow;
- one-off task narratives.

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
- for each new memory: <memory>the fact</memory>
  (project-scoped: <memory scope="project">the fact</memory>)
- to rewrite a memory: <memory id="m3">the corrected fact</memory>
- to remove a memory: <delete-memory id="m3"/>
- to propose a model-guidance edit (queued for the replay gate, not applied):
  <guidance stem="glm">
  {"content": "full replacement guidance file",
   "why": "one line of journal evidence",
   "motivation": {"prompt": "task that reproduced the failure",
                  "setup": {"relative/path.py": "seed file content the task needs"},
                  "checks": [{"kind": "tool_called", "name": "ToolTheFlowUses"},
                             {"kind": "not_contains", "pattern": "regex of the failure's phrasing"}]}}
  </guidance>
- if nothing is worth keeping: NO_CHANGES
"""

_CONSOLIDATE_SYSTEM = """\
You curate the long-term memory and skill library of a code-mode agent
harness. This is a periodic consolidation pass: no new session evidence, just
the full current store with usage data. Improve its SHAPE:

- merge near-duplicate memories into one entry (rewrite one with its id,
  delete the others);
- generalize entries that captured one narrow instance of a broader fact;
- delete entries that contradict each other (keep the better-evidenced one),
  restate stale-looking absolutes more carefully, and drop task narratives
  that never belonged in memory;
- an entry with zero recalls and old timestamps is a candidate for deletion,
  but low usage alone is not proof of uselessness — prefer merging over
  deleting when the fact still looks true;
- for skills: delete ones that are superseded by a better one; do NOT write
  new skills in this pass.

A small, sharp store beats a large, stale one — but do not destroy
information wholesale: most passes change a handful of entries at most.

Output format — nothing else:
- to rewrite a memory: <memory id="m3">the merged/corrected fact</memory>
  (add scope="project" when the fact is project-specific)
- to remove a memory: <delete-memory id="m3"/>
- to remove a skill: <delete-skill name="snake_case_name"/>
- if the store is already in good shape: NO_CHANGES
"""

_SKILL_RE = re.compile(r'<skill name="([a-z0-9_]+)">\n?(.*?)</skill>', re.DOTALL)
_DELETE_RE = re.compile(r'<delete-skill name="([a-z0-9_]+)"\s*/>')
_MEMORY_RE = re.compile(
    r'<memory(?:\s+id="(m\d+)")?(?:\s+scope="([^"]*)")?\s*>\n?(.*?)</memory>',
    re.DOTALL,
)
_DELETE_MEMORY_RE = re.compile(r'<delete-memory id="(m\d+)"\s*/>')
_GUIDANCE_RE = re.compile(
    r'<guidance stem="([^"]{1,64})">\n?(.*?)</guidance>', re.DOTALL
)

# Journal previews are already trimmed at write time; cap the slice we show.
MAX_ENTRIES_PER_PASS = 40
MIN_NEW_ENTRIES = 8
# Consolidation (a whole-store curation pass) is interval-gated like skill
# curation; it rides an ordinary background pass, never session close.
CONSOLIDATE_INTERVAL_DAYS = 7.0
CONSOLIDATE_MIN_MEMORIES = 8


class Synthesizer:
    def __init__(
        self,
        provider: Any,
        model: str,
        store: SkillStore,
        *,
        project: str | None = None,
        on_change: Any | None = None,  # callable(list[str]) -> None|Awaitable
        min_new_entries: int = MIN_NEW_ENTRIES,
        max_entries: int = MAX_ENTRIES_PER_PASS,
        max_tokens: int = 4096,
        pass_timeout: float = 180.0,
        close_timeout: float = 90.0,
        session_model: str | None = None,
        guidance_snapshot: dict[str, str] | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._store = store
        # The current project's memory-scope key: ``scope="project"`` tags in
        # the model's output resolve to it. ``None`` keeps everything global.
        self._project = project
        # Phase 2.3: the SESSION's model id (may differ from the synthesis
        # model above) — a guidance proposal's stem must be a substring of it
        # to ever load — and the current guidance file(s) matching it, shown
        # in the prompt so proposals amend rather than overwrite blindly.
        # ``None`` disables guidance proposals entirely (embedding hosts that
        # don't wire it keep today's behavior).
        self._session_model = session_model
        self._guidance_snapshot = dict(guidance_snapshot or {})
        self._on_change = on_change
        self._min_new = max(1, min_new_entries)
        self._max_entries = max_entries
        self._max_tokens = max_tokens
        self._task: asyncio.Task | None = None
        # A pass is one model call plus verification kernels; bound it so a
        # stalled stream can never wedge a turn or, worse, session close.
        self._pass_timeout = pass_timeout
        self._close_timeout = close_timeout
        # Set by the Remember channel: a pending explicit remember request
        # lowers the trigger threshold to "any new entries at all", so a
        # user's "remember this" lands before the session's eighth event
        # instead of maybe never.
        self._urgent = False
        self.last_error: str | None = None

    # ─── triggering ──────────────────────────────────────────────────────
    def mark_urgent(self) -> None:
        """A high-salience entry (Remember) is waiting: synthesize ASAP."""
        self._urgent = True

    def due(self) -> bool:
        new = self._store.journal_count() - self._store.synth_cursor()
        if self._urgent and new >= 1:
            return True
        return new >= self._min_new

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
        try:
            await asyncio.wait_for(self._drain_inner(), timeout=self._close_timeout)
        except TimeoutError:
            self.last_error = (
                f"synthesis on close exceeded {self._close_timeout:.0f}s and was abandoned"
            )
            # An abandoned pass leaves no cursor movement (the slice retries
            # next session) — but it must leave a trace, or a slow pass is
            # indistinguishable from one that never ran.
            self._log_pass("synthesis", [], error=self.last_error)
            if self._task is not None:
                self._task.cancel()
                self._task = None

    async def _drain_inner(self) -> None:
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
            changed = await asyncio.wait_for(self.run(), timeout=self._pass_timeout)
        except TimeoutError:
            self.last_error = f"synthesis pass exceeded {self._pass_timeout:.0f}s"
            self._log_pass("synthesis", [], error=self.last_error)
            return []
        except Exception as exc:  # never let synthesis break the session
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log_pass("synthesis", [], error=self.last_error)
            return []
        if self._consolidation_due():
            try:
                changed += await asyncio.wait_for(
                    self.consolidate(), timeout=self._pass_timeout
                )
            except Exception:  # consolidation is opportunistic, never fatal
                pass
        await self._notify(changed)
        return changed

    async def _notify(self, changed: list[str]) -> None:
        """Tell the host what landed (events/UI). Best-effort, never raises."""
        if not changed or self._on_change is None:
            return
        try:
            result = self._on_change(list(changed))
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    # ─── one synthesis pass ──────────────────────────────────────────────
    async def run(self) -> list[str]:
        """One pass over the un-synthesized journal slice. Returns saved names."""
        cursor = self._store.synth_cursor()
        entries = self._store.journal_entries(cursor, self._max_entries)
        if not entries:
            return []
        text = await self._complete(self._prompt(entries), _SYNTH_SYSTEM)
        changed = await self._apply(text)  # sets last_error (None on a clean pass)
        self._store.set_synth_cursor(cursor + len(entries))
        # The urgency flag covered the slice just consumed; a new Remember
        # re-arms it.
        self._urgent = False
        self._log_pass(
            "synthesis", changed, error=self.last_error,
            consumed=len(entries), output_preview=text,
        )
        return changed

    def _log_pass(
        self,
        kind: str,
        changed: list[str],
        *,
        error: str | None = None,
        consumed: int | None = None,
        output_preview: str | None = None,
    ) -> None:
        """Append one pass record to ``.synthesis-log.jsonl`` (best-effort).

        ``last_error`` dies with the process, and a background pass that
        silently fails — or silently decides NO_CHANGES — is otherwise
        indistinguishable from one that never ran. The log is the audit
        trail: when it ran, what it consumed, what changed, what went wrong.
        """
        record: dict[str, Any] = {"ts": time.time(), "kind": kind, "changed": changed}
        if consumed is not None:
            record["consumed"] = consumed
        if error:
            record["error"] = error
        if output_preview is not None and not changed:
            # Only when nothing landed: the raw head is what explains why.
            record["output_head"] = " ".join(output_preview.split())[:400]
        try:
            with (self._store.root / ".synthesis-log.jsonl").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ─── consolidation (whole-store curation, interval-gated) ────────────
    def _consolidation_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if len(self._store.memories()) < CONSOLIDATE_MIN_MEMORIES:
            return False
        return (
            now - self._store.last_consolidated_at()
            >= CONSOLIDATE_INTERVAL_DAYS * 86_400
        )

    async def consolidate(self) -> list[str]:
        """One whole-store curation pass: merge, generalize, retire.

        Sees every memory (with usage counts) and the skill index — no journal
        slice. Only rewrites and deletions apply; a ``<skill>`` tag in the
        output is ignored, so a confused pass cannot inject unproven code.
        """
        self._store.set_last_consolidated_at(time.time())
        lines = ["## Memory store (id, recalls, age, scope, text)"]
        now = time.time()
        memories = self._store.memories()
        if not memories:
            lines.append("(empty)")
        for m in memories:
            age_days = (now - (m.updated_at or m.created_at or now)) / 86_400
            lines.append(
                f"[{m.id}] recalls={m.uses} age={age_days:.0f}d "
                f"scope={m.scope}: {m.text}"
            )
        lines.append("")
        lines.append("## Skill library (name, uses, description)")
        skills = self._store.list()
        if not skills:
            lines.append("(empty)")
        for s in skills:
            lines.append(f"- {s.name} (used {s.uses}x): {s.description}")
        text = await self._complete("\n".join(lines), _CONSOLIDATE_SYSTEM)
        changed: list[str] = []
        for match in _MEMORY_RE.finditer(text or ""):
            memory_id, scope_attr, memory_text = match.groups()
            if memory_id is None:
                continue  # consolidation rewrites; it does not add
            try:
                saved = self._store.save_memory(
                    memory_text.strip(),
                    memory_id=memory_id,
                    scope=self._resolve_scope(scope_attr),
                )
            except ValueError:
                continue
            changed.append(f"memory:{saved.id}")
        for match in _DELETE_MEMORY_RE.finditer(text or ""):
            if self._store.delete_memory(match.group(1)):
                changed.append(f"-memory:{match.group(1)}")
        for match in _DELETE_RE.finditer(text or ""):
            if self._store.delete(match.group(1)):
                changed.append(f"-{match.group(1)}")
        self._log_pass("consolidation", changed, output_preview=text)
        return changed

    def _resolve_scope(self, scope_attr: str | None) -> str | None:
        """Map a tag's scope attribute to a stored scope value.

        ``scope="project"`` resolves to this session's project key;
        ``scope="global"`` pins global; absent leaves existing scope alone
        (None → save_memory keeps/defaults).
        """
        if not scope_attr:
            return None
        if scope_attr == "project":
            return self._project or "global"
        return "global"

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
        lines.append("## Current memory (id, recalls, scope, text)")
        memories = self._store.memories()
        if not memories:
            lines.append("(empty)")
        for m in memories:
            lines.append(f"[{m.id}] recalls={m.uses} scope={m.scope}: {m.text}")
        if self._session_model is not None:
            lines.append("")
            lines.append(f"## Session model: {self._session_model}")
            if self._guidance_snapshot:
                lines.append("Current model-guidance file(s) — a proposal replaces the whole file:")
                for stem, content in sorted(self._guidance_snapshot.items()):
                    lines.append(f"### {stem}.md")
                    lines.append(content)
            else:
                lines.append("(no model-guidance file exists for this model yet)")
        lines.append("")
        lines.append("## Session journal (conversation + executed cells, in order)")
        cell_no = 0
        for e in entries:
            kind = e.get("kind", "cell")
            if kind in ("user", "assistant", "remember"):
                scope_hint = (
                    f" (scope={e['scope']})"
                    if kind == "remember" and e.get("scope")
                    else ""
                )
                lines.append(f"[{kind}]{scope_hint} {str(e.get('text', '')).strip()}")
                continue
            if kind == "tool":
                # JSON-surface call (ToolJournal): one compact line.
                outcome = "error" if e.get("error") else str(e.get("output", "")).strip()
                lines.append(
                    f"[tool] {e.get('name')}({str(e.get('input', '')).strip()}) "
                    f"-> {outcome}"
                )
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

    async def _complete(self, prompt: str, system: str = _SYNTH_SYSTEM) -> str:
        request = ProviderRequest(
            model=self._model,
            messages=[Message(role="user", content=[Text(text=prompt)])],
            system=system,
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
        for match in _MEMORY_RE.finditer(text or ""):
            memory_id, scope_attr, memory_text = match.groups()
            try:
                saved = self._store.save_memory(
                    memory_text.strip(),
                    memory_id=memory_id,
                    scope=self._resolve_scope(scope_attr),
                )
            except ValueError as exc:
                errors.append(f"memory: {exc}")
                continue
            changed.append(f"memory:{saved.id}")
        for match in _DELETE_MEMORY_RE.finditer(text or ""):
            if self._store.delete_memory(match.group(1)):
                changed.append(f"-memory:{match.group(1)}")
        for match in _GUIDANCE_RE.finditer(text or ""):
            # Phase 2.3: guidance edits are PROPOSED, never applied here.
            # Queue-time validation is cheap; the commit gate (validity →
            # improvement → retention over the anchor set) runs out-of-band
            # via `fluid evolve run`, because gating replays a live model.
            stem, raw = match.group(1), match.group(2).strip()
            if self._session_model is None:
                errors.append(f"guidance:{stem}: no session model wired")
                continue
            try:
                body = json.loads(raw)
            except ValueError as exc:
                errors.append(f"guidance:{stem}: body is not valid JSON ({exc})")
                continue
            from fluid.codemode.evolution import (
                draft_guidance_proposal,
                queue_pending,
            )

            result = draft_guidance_proposal(
                stem=stem, body=body, session_model=self._session_model,
            )
            if isinstance(result, str):
                errors.append(f"guidance:{stem}: {result}")
                continue
            queue_pending(self._store.root, result)
            changed.append(f"guidance-proposal:{result.target}")
        self.last_error = "; ".join(errors) if errors else None
        return changed

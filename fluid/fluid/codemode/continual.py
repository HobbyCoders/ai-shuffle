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
is recorded on ``last_error``, written to ``.synthesis-log.jsonl`` and logged,
then swallowed. The journal cursor only advances after a successful pass, so
failed slices are retried — after a backoff shared by every session over the
store, so a provider that rejects every request cannot spend the day's
budget in minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from fluid.codemode.skills import (
    MAX_LOG_BYTES,
    MAX_LOG_ENTRIES,
    SkillStore,
    journal_lock,
    referenced_skills,
    trim_jsonl,
    validate_skill,
)
from fluid.codemode.verify import verify_sources
from fluid.messages.content import Message, Text
from fluid.providers.base import ProviderRequest

_log = logging.getLogger(__name__)

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
it) interleaved with the cells that were executed. Several sessions may share
one journal (a parent and its subagents, two shells on one machine); a
`--- session ... ---` line marks where one narrative ends and another begins,
so never read across one as if it were a single story. A skill listed without
its source this pass can be deleted but not rewritten. Distill only *genuinely
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

A journal entry marked [friction] is the HARNESS's own mechanical record of a
call that wasted work — a path that did not exist, one tool failing over and
over, content re-read after it was already delivered, a search that found
nothing re-run with near-identical arguments. It carries facts, never the
agent's interpretation, and deciding what (if anything) it means is your job.
Friction that repeats usually points at a durable fact about this environment
worth a memory (where an output actually arrives, what a path is really
called); friction that is the model mis-driving the harness the same way twice
belongs in a model-guidance proposal. A single one-off is noise — say nothing,
and never turn friction into a negative claim about a tool.

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
- a memory listed as "explicit" was saved on the user's request: never
  rewrite or delete it, and do not add a new memory that repeats it
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
# Attributes in any order: a model that writes ``scope`` before ``id`` (or
# adds one we do not use) produced a tag the old fixed-order pattern skipped
# without a trace.
_MEMORY_RE = re.compile(r'<memory((?:\s+[a-z_]+="[^"]*")*)\s*>\n?(.*?)</memory>', re.DOTALL)
_ATTR_RE = re.compile(r'([a-z_]+)="([^"]*)"')
_MEMORY_ID_RE = re.compile(r"[A-Za-z0-9_:.\-]{1,160}")
_DELETE_MEMORY_RE = re.compile(r'<delete-memory id="([A-Za-z0-9_:.\-]{1,160})"\s*/>')
_GUIDANCE_RE = re.compile(
    r'<guidance stem="([^"]{1,64})">\n?(.*?)</guidance>', re.DOTALL
)

_WORD_RE = re.compile(r"[a-z0-9]{4,}")
# Journal kinds whose text a user wrote (or asked to have remembered).
_USER_KINDS = ("user", "remember")
_PROVENANCE_MATCH = 0.5


def _memory_tags(text: str) -> list[tuple[str | None, str | None, str, str | None]]:
    """``(id, scope, body, problem)`` for every ``<memory>`` tag in ``text``.

    ``problem`` is set (and the tag must be skipped) when the id attribute is
    malformed; it lands in the pass's error list instead of vanishing.
    """
    out: list[tuple[str | None, str | None, str, str | None]] = []
    for match in _MEMORY_RE.finditer(text or ""):
        attrs = dict(_ATTR_RE.findall(match.group(1) or ""))
        memory_id = attrs.get("id")
        problem = None
        if memory_id is not None and not _MEMORY_ID_RE.fullmatch(memory_id):
            problem = f"memory: malformed id {memory_id[:40]!r}"
        out.append((memory_id, attrs.get("scope"), match.group(2), problem))
    return out


def _content_words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _entry_tool_text(entry: dict) -> str:
    kind = entry.get("kind", "cell")
    if kind == "tool":
        return str(entry.get("output", ""))
    if kind == "cell":
        parts = [str(entry.get(k, "") or "") for k in ("stdout", "value", "error")]
        return " ".join(parts)
    if kind == "friction":
        return str(entry.get("text", ""))
    return ""


def memory_provenance(text: str, entries: list[dict]) -> str:
    """Where a proposed memory's words came from in the slice that produced it.

    ``user`` when at least half its content words appear in what the user
    wrote (user prompts, Remember entries); otherwise ``tool`` when they
    appear in tool or cell output; otherwise ``session``. Mechanical on
    purpose: the model that proposed the memory is the one an injected
    document may have steered, so it does not get to vouch for itself.
    """
    words = _content_words(text)
    if not words:
        return "session"
    user_words = _content_words(" ".join(
        str(e.get("text", "")) for e in entries if e.get("kind") in _USER_KINDS
    ))
    if len(words & user_words) / len(words) >= _PROVENANCE_MATCH:
        return "user"
    tool_words = _content_words(" ".join(_entry_tool_text(e) for e in entries))
    if len(words & tool_words) / len(words) >= _PROVENANCE_MATCH:
        return "tool"
    return "session"


def _add_usage(total: dict[str, int], raw: dict) -> None:
    """Fold one model call's usage into ``total`` (provider key spellings
    normalized to fluid's four counters)."""
    for key, aliases in (
        ("input_tokens", ("input_tokens", "prompt_tokens")),
        ("output_tokens", ("output_tokens", "completion_tokens")),
        ("cache_read_tokens", ("cache_read_input_tokens", "cache_read_tokens")),
        ("cache_creation_tokens", ("cache_creation_input_tokens", "cache_creation_tokens")),
    ):
        for alias in aliases:
            value = raw.get(alias)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
                total[key] = total.get(key, 0) + int(value)
                break

class SynthesisIncomplete(RuntimeError):
    """The model call finished without a usable answer.

    Raised by :meth:`Synthesizer._complete` when the stream stopped for
    ``max_tokens`` or produced no text at all. Either way the slice was not
    judged, so the caller must not advance the cursor: an empty completion
    used to parse as "no tags" and count as a clean NO_CHANGES pass, which is
    how ~557 passes wrote nothing while burning the daily budget.
    """

    def __init__(self, stop_reason: str | None, usage: dict[str, int], output_chars: int) -> None:
        self.stop_reason = stop_reason
        self.usage = dict(usage)
        self.output_chars = output_chars
        what = "no text" if output_chars == 0 else f"{output_chars} chars"
        super().__init__(
            f"synthesis returned {what} (stop_reason={stop_reason or 'unknown'}, "
            f"output_tokens={usage.get('output_tokens', 0)})"
        )


class SynthesisProviderError(RuntimeError):
    """The provider answered the learner's request with an error chunk.

    Providers report HTTP and stream failures as a ``kind="error"`` chunk
    rather than raising. The learner used to ignore that chunk, so a
    rejected request looked like an empty completion ("synthesis returned no
    text (stop_reason=unknown, output_tokens=0)") and the provider's reason
    was thrown away. The desktop worker logged 273 such rows; per local day
    their count matches the app log's HTTP 400 answers from the Codex
    backend exactly (96, 48, 48 on 2026-09-13/14/15-16).
    """

    def __init__(self, payload: dict[str, Any], usage: dict[str, int]) -> None:
        self.status = payload.get("status")
        self.usage = dict(usage)
        self.output_chars = 0
        self.stop_reason = None
        message = " ".join(str(payload.get("message") or payload.get("type") or "").split())
        self.detail = message[:300]
        where = f"HTTP {self.status}" if self.status else "stream error"
        super().__init__(f"provider error ({where}): {self.detail or 'no message'}")


class InferenceBudgetExhausted(RuntimeError):
    """The store's daily inference-call budget is spent (see facts.py)."""


# Journal previews are already trimmed at write time; cap the slice we show.
MAX_ENTRIES_PER_PASS = 40
# The request's user-message size. Split so the journal always fits: the old
# prompt put the skill library and the whole memory list first and then cut
# the text at 32,000 characters, so a store with 95 facts (33,274 characters
# of memory lines on the desktop worker) sent every pass without a single
# journal line. The model answered NO_CHANGES and the cursor advanced over
# evidence nobody read. Now memory lines have a fixed budget, the skill
# library a reserve, and the journal the rest (never less than the minimum);
# the library then fills whatever the journal left unused.
PROMPT_CHAR_LIMIT = 32_000
MEMORY_PROMPT_CHARS = 6_000
LIBRARY_PROMPT_RESERVE = 8_000
JOURNAL_MIN_PROMPT_CHARS = 8_000
# Consolidation lists only facts it may change (inferred, not explicit).
CONSOLIDATE_MEMORY_CHARS = 24_000
# Rough cost of a "--- session ... ---" divider when sizing the journal.
_SESSION_LABEL_ALLOWANCE = 80
# Failed passes back off per model, shared through meta.json: 5 min, 10,
# 20, ... up to 6 h. A deterministic rejection then costs about a dozen
# calls a day instead of the whole 48-call budget.
FAILURE_BACKOFF_BASE = 300.0
FAILURE_BACKOFF_MAX = 6 * 3600.0
# A failed consolidation waits this long before its next attempt instead of
# riding (and doubling the cost of) every following pass.
CONSOLIDATE_RETRY_SECONDS = 6 * 3600.0
# How often one synthesizer may record that it found the synthesis lock
# held for longer than a pass can legitimately take.
LOCK_BUSY_LOG_INTERVAL = 600.0
# A pass costs one of the day's 48 inference calls (facts.MAX_INFERENCE_CALLS_
# PER_DAY), so it has to see a real slice: a turn writes its user prompt, its
# assistant reply and a handful of cells, so 20 is roughly three turns of
# evidence. At 8 the container spent the whole budget by late morning on
# slices of three to eight entries.
MIN_NEW_ENTRIES = 20
# Output room for a pass. The old 4,096 cap was consumed by reasoning alone
# on gpt-6-class models, so the stream ended ``incomplete`` with no text and
# the pass counted as a clean NO_CHANGES. Overridable through the
# constructor (``max_tokens``) for hosts that pin a smaller model.
SYNTH_MAX_TOKENS = 16_000
# Reasoning effort sent on every synthesis request. Low: the task is a
# read-and-classify over a bounded journal, and the budget is calls per day,
# not quality per call. Serializers map it per provider (Anthropic
# output_config.effort, OpenAI-shaped reasoning.effort).
SYNTH_EFFORT = "low"
# Skill source shown per pass: skills the slice referenced are always shown
# whole; the rest fill this budget most-used first and beyond it appear as an
# index line only. Every pass used to re-send every skill's full source.
SKILL_SOURCE_BUDGET = 24_000
# Lock file (next to the journal) held for the whole pass, so two processes
# over one store never pay for the same slice or write the same memory twice.
SYNTHESIS_LOCK = "synthesis"
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
        max_tokens: int = SYNTH_MAX_TOKENS,
        effort: str | None = SYNTH_EFFORT,
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
        self._effort = effort
        self._task: asyncio.Task | None = None
        # UTC day (time // 86400) on which the daily budget was found spent.
        # Until the day rolls over ``due()`` is False, so a kick per cell
        # does not append hundreds of identical budget rows to the pass log.
        self._budget_spent_day: int | None = None
        # The last completion's stop reason, for the pass log.
        self.last_stop_reason: str | None = None
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
        # Spend: the last pass's tokens and the running total for this
        # synthesizer's life. Both land in the pass log; the loader puts the
        # last pass on the ContinualUpdate event.
        self.last_pass_usage: dict[str, int] = {}
        self.usage: dict[str, int] = {}
        # Memories the last pass saved with tool-only provenance, as
        # ``"<id>: <text>"``: stored, held out of the prompt, surfaced to the
        # host so the user can confirm or ignore them.
        self.last_held: list[str] = []
        # Outcome of the last run(): a failed or skipped pass must not be
        # followed by a consolidation call (each failed pass on the desktop
        # worker was followed by a failed consolidation, 24 + 24 calls a
        # day, the whole budget).
        self.last_pass_failed = False
        self.last_pass_skipped = False
        # When this synthesizer first found the synthesis lock held, and when
        # it last wrote that down (see run()).
        self._lock_busy_since: float | None = None
        self._lock_logged_at = float("-inf")
        self._kick_warned = False

    # ─── triggering ──────────────────────────────────────────────────────
    def mark_urgent(self) -> None:
        """A high-salience entry (Remember) is waiting: synthesize ASAP."""
        self._urgent = True

    def due(self) -> bool:
        if self._budget_spent_day is not None:
            if int(time.time() // 86400) == self._budget_spent_day:
                return False
            self._budget_spent_day = None
        try:
            backoff = self._store.learner_backoff(self._model)
        except Exception:
            backoff = {}
        if backoff and time.time() < float(backoff.get("until", 0.0)):
            return False
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
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Callers swallow exceptions from kick(), so without this line a
            # host that journals from a worker thread would never learn and
            # never say why.
            if not self._kick_warned:
                self._kick_warned = True
                _log.warning(
                    "Memory learner: a pass is due but kick() was called with "
                    "no running event loop; synthesis will wait for the next "
                    "kick from the loop thread or session close"
                )
            return
        self._task = loop.create_task(self._run_safe())

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
            retry = self._note_failure(self.last_error)
            self._log_pass("synthesis", [], error=self.last_error, retry_after_s=retry)
            return []
        except InferenceBudgetExhausted as exc:
            # The slice stays unconsumed (cursor untouched, journal lines
            # retained by reap) and is retried once the UTC day rolls over.
            self._budget_spent_day = int(time.time() // 86400)
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log_pass("synthesis", [], error=self.last_error)
            return []
        except Exception as exc:  # never let synthesis break the session
            self.last_error = f"{type(exc).__name__}: {exc}"
            retry = self._note_failure(self.last_error)
            self._log_pass("synthesis", [], error=self.last_error, retry_after_s=retry)
            return []
        if not (self.last_pass_failed or self.last_pass_skipped) and self._consolidation_due():
            try:
                changed += await asyncio.wait_for(
                    self._consolidate_locked(), timeout=self._pass_timeout
                )
            except InferenceBudgetExhausted as exc:
                self._budget_spent_day = int(time.time() // 86400)
                self._log_pass("consolidation", [], error=f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # consolidation is opportunistic, never fatal
                self._consolidation_failed(f"{type(exc).__name__}: {exc}")
        await self._notify(changed)
        return changed

    def _note_failure(self, error: str) -> float:
        """Record a failed pass in the store's shared backoff; returns the
        seconds until this model's next attempt (0 when it could not be
        recorded)."""
        self.last_pass_failed = True
        now = time.time()
        try:
            entry = self._store.note_learner_failure(
                self._model, error, now=now,
                base=FAILURE_BACKOFF_BASE, cap=FAILURE_BACKOFF_MAX,
            )
        except Exception:
            return 0.0
        return max(0.0, float(entry.get("until", now)) - now)

    def _consolidation_failed(self, error: str, **fields: Any) -> None:
        """A failed consolidation waits ``CONSOLIDATE_RETRY_SECONDS``."""
        self.last_error = error
        try:
            self._store.set_consolidation_retry_at(time.time() + CONSOLIDATE_RETRY_SECONDS)
        except Exception:
            pass
        self._log_pass(
            "consolidation", [], error=error,
            retry_after_s=CONSOLIDATE_RETRY_SECONDS, **fields,
        )

    async def _consolidate_locked(self) -> list[str]:
        """Consolidate under the synthesis lock, so two sessions that finish
        a pass together do not both pay for the same curation."""
        with journal_lock(self._store.root / SYNTHESIS_LOCK) as held:
            if not held:
                return []
            return await self.consolidate()

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
        """One pass over the un-synthesized journal slice. Returns saved names.

        Single-flight across processes: the pass holds the store's synthesis
        lock from reading the cursor to advancing it, and a pass that finds
        the lock taken does nothing, so two engines over one store never pay
        twice for a slice or write near-duplicate memories from it.
        """
        self.last_pass_failed = False
        self.last_pass_skipped = False
        with journal_lock(self._store.root / SYNTHESIS_LOCK) as held:
            if not held:
                self.last_pass_skipped = True
                self._note_lock_busy()
                return []
            self._lock_busy_since = None
            return await self._run_locked()

    def _note_lock_busy(self) -> None:
        """Another pass holds the synthesis lock. Normal while it runs; a
        hold longer than a pass may take means a wedged holder, and before
        this line that state was completely silent (no pass row at all)."""
        now = time.monotonic()
        if self._lock_busy_since is None:
            self._lock_busy_since = now
        busy = now - self._lock_busy_since
        if busy < self._pass_timeout or now - self._lock_logged_at < LOCK_BUSY_LOG_INTERVAL:
            return
        self._lock_logged_at = now
        self._log_pass(
            "synthesis", [],
            error=(
                f"skipped: {SYNTHESIS_LOCK}.lock has been held by another pass for "
                f"at least {busy:.0f}s (longer than the {self._pass_timeout:.0f}s "
                "pass limit); a holder may be wedged"
            ),
        )

    async def _run_locked(self) -> list[str]:
        cursor = self._store.synth_cursor()
        forgotten_before = self._store._facts.snapshot().get("forgotten_before", 0)
        indexed, reached = self._store.journal_slice_indexed(cursor, self._max_entries)
        indexed = [(n, e) for n, e in indexed if e.get("ts", 0) > forgotten_before]
        if not indexed:
            if reached > cursor:
                # Only malformed lines in the slice: step past them, or every
                # later pass would stop at the same corrupt line.
                self._store.set_synth_cursor(reached)
            return []
        # Only what the prompt can show is consumed: the rest of the slice
        # stays behind the cursor for the next pass.
        fitted = self._fit_journal(indexed)
        deferred = len(indexed) - len(fitted)
        if deferred:
            reached = fitted[-1][0]
        entries = [e for _, e in fitted]
        groups: dict[str | None, list[dict]] = {}
        for entry in entries:
            groups.setdefault(entry.get("project"), []).append(entry)
        changed = []
        usage = {}
        text = ""
        original_project = self._project
        try:
            for project, group in groups.items():
                # Unattributed legacy input may produce global facts, but must
                # not be assigned to whichever project happens to run next.
                self._project = project or "legacy:unassigned"
                try:
                    text, group_usage = await self._complete(self._prompt(group), _SYNTH_SYSTEM)
                except (SynthesisIncomplete, SynthesisProviderError) as exc:
                    # Not a verdict on the slice: leave the cursor where it
                    # is so the next pass retries it (after the backoff),
                    # and log what came back so a rejected or empty stream
                    # is never mistaken for NO_CHANGES again.
                    for key, value in exc.usage.items():
                        usage[key] = usage.get(key, 0) + value
                    self.last_error = str(exc)
                    retry = self._note_failure(self.last_error)
                    self._log_pass(
                        "synthesis", changed, error=self.last_error,
                        consumed=0, output_chars=exc.output_chars,
                        usage=usage, stop_reason=exc.stop_reason,
                        provider_status=getattr(exc, "status", None),
                        retry_after_s=retry,
                    )
                    return changed
                for key, value in group_usage.items():
                    usage[key] = usage.get(key, 0) + value
                if self._store._facts.snapshot().get("forgotten_before", 0) > forgotten_before:
                    return []
                changed.extend(await self._apply(text, group))
        finally:
            self._project = original_project
        self._store.set_synth_cursor(reached)
        try:
            self._store.clear_learner_backoff(self._model)
        except Exception:
            pass
        # Consumed entries have no reader left: drop the ones past the
        # retention tail and rebase the cursor. Best-effort — a failed reap
        # must never cost us the pass that just succeeded.
        try:
            self._store.reap_journal()
        except Exception:
            pass
        # The urgency flag covered the slice just consumed; a new Remember
        # re-arms it (unless part of the slice was deferred).
        self._urgent = self._urgent and bool(deferred)
        self._log_pass(
            "synthesis", changed, error=self.last_error,
            consumed=len(entries), output_preview=text, usage=usage,
            stop_reason=self.last_stop_reason, deferred=deferred or None,
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
        usage: dict[str, int] | None = None,
        stop_reason: str | None = None,
        output_chars: int | None = None,
        provider_status: int | None = None,
        retry_after_s: float | None = None,
        deferred: int | None = None,
    ) -> None:
        """Append one pass record to ``.synthesis-log.jsonl`` (best-effort).

        ``last_error`` dies with the process, and a background pass that
        silently fails — or silently decides NO_CHANGES — is otherwise
        indistinguishable from one that never ran. The log is the audit
        trail: when it ran, what it consumed, what changed, what went wrong.
        The same outcome goes to the process log (warning on failure), so a
        broken learner shows up where operators already look.

        Bounded like everything else the store owns: once the file grows past
        ``MAX_LOG_BYTES`` it is rotated down to the most recent
        ``MAX_LOG_ENTRIES`` passes.
        """
        record: dict[str, Any] = {"ts": time.time(), "kind": kind, "changed": changed}
        record["model"] = self._model
        if consumed is not None:
            record["consumed"] = consumed
        if deferred:
            record["deferred"] = int(deferred)
        if usage:
            record["usage"] = dict(usage)
        if error:
            record["error"] = error
        if provider_status is not None:
            record["provider_status"] = provider_status
        if retry_after_s:
            record["retry_after_s"] = round(float(retry_after_s))
        if output_chars is not None:
            record["output_chars"] = int(output_chars)
        elif output_preview is not None:
            record["output_chars"] = len(output_preview)
        if usage and "output_tokens" in usage:
            record["output_tokens"] = int(usage["output_tokens"])
        if stop_reason:
            record["stop_reason"] = stop_reason
        if error and consumed:
            # The pass ran; some proposals were rejected by validation.
            _log.warning(
                "Memory learner %s pass (model=%s): consumed %s, changed %s, rejected: %s",
                kind, self._model, consumed, changed or "nothing", error,
            )
        elif error:
            _log.warning(
                "Memory learner %s pass failed (model=%s, store=%s): %s%s",
                kind, self._model, self._store.root, error,
                f"; next attempt in {retry_after_s:.0f}s" if retry_after_s else "",
            )
        else:
            _log.info(
                "Memory learner %s pass (model=%s): consumed %s, changed %s",
                kind, self._model, consumed, changed or "nothing",
            )
        path = self._store.root / ".synthesis-log.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return
        try:
            if path.stat().st_size > MAX_LOG_BYTES:
                trim_jsonl(path, MAX_LOG_ENTRIES)
        except OSError:
            pass

    # ─── consolidation (whole-store curation, interval-gated) ────────────
    def _consolidation_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        # Only inferred facts can be merged or retired; a store of explicit
        # saves has nothing for this pass to do.
        editable = [m for m in self._store.memories() if not m.protected]
        if len(editable) < CONSOLIDATE_MIN_MEMORIES:
            return False
        if now < self._store.consolidation_retry_at():
            return False
        return (
            now - self._store.last_consolidated_at()
            >= CONSOLIDATE_INTERVAL_DAYS * 86_400
        )

    async def consolidate(self) -> list[str]:
        """One whole-store curation pass: merge, generalize, retire.

        Sees the facts it may change (inferred, with usage counts) and the
        skill index — no journal slice. Only rewrites and deletions apply; a
        ``<skill>`` tag in the output is ignored, so a confused pass cannot
        inject unproven code.

        This is also where the store's storage bounds are enforced: the
        deterministic reap runs first, so the model curates a store that is
        already trimmed and never sees (or controls) the reaping itself.
        """
        try:
            self._store.reap()
        except Exception:  # cleanup is opportunistic, never fatal
            pass
        lines = ["## Memory store (id, recalls, age, scope, text)"]
        now = time.time()
        memories = [m for m in self._store.memories() if not m.protected]
        revisions = {m.id: m.revision for m in memories}
        memories.sort(key=lambda m: -(m.updated_at or m.created_at or 0.0))
        budget = CONSOLIDATE_MEMORY_CHARS
        omitted = 0
        if not memories:
            lines.append("(empty)")
        for m in memories:
            age_days = (now - (m.updated_at or m.created_at or now)) / 86_400
            line = (
                f"[{m.id}] recalls={m.uses} age={age_days:.0f}d "
                f"scope={m.scope}: {m.text}"
            )
            if len(line) + 1 > budget:
                omitted += 1
                continue
            lines.append(line)
            budget -= len(line) + 1
        if omitted:
            lines.append(f"({omitted} older facts not listed this pass)")
        lines.append("")
        lines.append("## Skill library (name, uses, description)")
        skills = self._store.list()
        if not skills:
            lines.append("(empty)")
        for s in skills:
            lines.append(f"- {s.name} (used {s.uses}x): {s.description}")
        try:
            text, usage = await self._complete("\n".join(lines), _CONSOLIDATE_SYSTEM)
        except (SynthesisIncomplete, SynthesisProviderError) as exc:
            self._consolidation_failed(
                str(exc), usage=exc.usage, output_chars=exc.output_chars,
                stop_reason=exc.stop_reason,
                provider_status=getattr(exc, "status", None),
            )
            return []
        changed: list[str] = []
        errors: list[str] = []
        for memory_id, scope_attr, memory_text, problem in _memory_tags(text):
            if problem:
                errors.append(problem)
                continue
            if memory_id not in revisions:
                continue  # no inserts, protected edits, or unknown snapshot IDs
            try:
                saved = self._store.save_memory(
                    memory_text.strip(),
                    memory_id=memory_id,
                    scope=self._resolve_scope(scope_attr),
                    expected_revision=revisions[memory_id],
                )
            except ValueError as exc:
                errors.append(f"memory {memory_id}: {exc}")
                continue
            changed.append(f"memory:{saved.id}")
        for match in _DELETE_MEMORY_RE.finditer(text or ""):
            if self._store.delete_memory(match.group(1)):
                changed.append(f"-memory:{match.group(1)}")
        for match in _DELETE_RE.finditer(text or ""):
            if self._store.delete(match.group(1)):
                changed.append(f"-{match.group(1)}")
        self._log_pass(
            "consolidation", changed, usage=usage,
            error="; ".join(errors) or None, consumed=len(memories),
            stop_reason=self.last_stop_reason,
        )
        self._store.set_last_consolidated_at(time.time())
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

    # ─── the synthesis prompt ────────────────────────────────────────────
    def _library_lines(self, entries: list[dict], budget: int | None = None) -> list[str]:
        """The skill library as the prompt shows it: skills the slice
        referenced in full, then the most used, until ``SKILL_SOURCE_BUDGET``
        characters of source are spent; the rest as an index line.

        ``budget`` bounds the whole section (headers, index lines and
        source); skills that do not fit even as an index line are counted.
        """
        lines = ["## Current skill library"]
        skills = self._store.list()
        if not skills:
            lines.append("(empty)")
            return lines
        limit = PROMPT_CHAR_LIMIT if budget is None else max(0, budget)
        used = len(lines[0]) + 1
        referenced: set[str] = set()
        for e in entries:
            if e.get("kind", "cell") == "cell":
                referenced.update(referenced_skills(str(e.get("code", ""))))
        ordered = sorted(
            skills, key=lambda s: (s.name not in referenced, -s.uses, s.name)
        )
        source_budget = SKILL_SOURCE_BUDGET
        omitted = 0
        for s in ordered:
            header = f"### skills.{s.name}  (used {s.uses}x)"
            source = s.source.rstrip("\n")
            whole = len(header) + len(source) + 16  # fences and newlines
            if (
                source
                and (s.name in referenced or len(source) <= source_budget)
                and used + whole <= limit
            ):
                lines += [header, "```python", source, "```"]
                source_budget -= len(source)
                used += whole
                continue
            index = (
                f"{header}: {s.description} (source not shown this pass; "
                f"{len(s.source)} bytes)"
            )
            if used + len(index) + 1 <= limit:
                lines.append(index)
                used += len(index) + 1
            else:
                omitted += 1
        if omitted:
            lines.append(f"({omitted} more skills not listed this pass)")
        return lines

    def _memory_lines(self) -> list[str]:
        """Saved facts the pass should know about, within
        ``MEMORY_PROMPT_CHARS``: this project's and global ones, newest
        first. Explicit saves are marked read-only. The old prompt listed
        every fact in every scope ahead of the journal and pushed the journal
        past the request's character cut."""
        lines = ["## Current memory (id, recalls, scope, text)"]
        project = self._project
        memories = [
            m for m in self._store.memories()
            if m.scope == "global" or (project is not None and m.scope == project)
        ]
        if not memories:
            lines.append("(empty)")
            return lines
        memories.sort(key=lambda m: -(m.updated_at or m.created_at or 0.0))
        budget = MEMORY_PROMPT_CHARS
        omitted = 0
        for m in memories:
            flag = " explicit" if m.protected else ""
            line = f"[{m.id}] recalls={m.uses} scope={m.scope}{flag}: {m.text}"
            if len(line) + 1 > budget:
                omitted += 1
                continue
            lines.append(line)
            budget -= len(line) + 1
        if omitted:
            lines.append(
                f"({omitted} older facts not listed; a new memory that repeats "
                "a saved fact word for word is merged, not duplicated)"
            )
        return lines

    @staticmethod
    def _render_entry(e: dict, cell_no: int) -> list[str]:
        """One journal entry as prompt lines (no session divider)."""
        kind = e.get("kind", "cell")
        if kind in ("user", "assistant", "remember", "friction"):
            scope_hint = (
                f" (scope={e['scope']})"
                if kind == "remember" and e.get("scope")
                else ""
            )
            return [f"[{kind}]{scope_hint} {str(e.get('text', '')).strip()}"]
        if kind == "tool":
            # JSON-surface call (ToolJournal): one compact line.
            outcome = "error" if e.get("error") else str(e.get("output", "")).strip()
            return [(
                f"[tool] {e.get('name')}({str(e.get('input', '')).strip()}) "
                f"-> {outcome}"
            )]
        lines = [
            f"### cell {cell_no}",
            "```python",
            str(e.get("code", "")).rstrip("\n"),
            "```",
        ]
        summary = {
            k: e[k]
            for k in ("stdout", "value", "error", "host_calls", "tools")
            if e.get(k)
        }
        if summary:
            lines.append(f"outcome: {json.dumps(summary, ensure_ascii=False)}")
        return lines

    def _entry_cost(self, e: dict) -> int:
        return len("\n".join(self._render_entry(e, 0))) + 1 + _SESSION_LABEL_ALLOWANCE

    def _journal_budget(self) -> int:
        """Characters the journal may use in one pass's prompt: the request
        limit minus the memory budget, the guidance section and a reserve
        for the skill library (only as much as the library needs)."""
        guidance = len("\n".join(self._guidance_lines()))
        try:
            library = sum(len(s.source) + 80 for s in self._store.list())
        except Exception:
            library = LIBRARY_PROMPT_RESERVE
        reserve = min(library, LIBRARY_PROMPT_RESERVE) + 200  # headers
        return max(
            JOURNAL_MIN_PROMPT_CHARS,
            PROMPT_CHAR_LIMIT - MEMORY_PROMPT_CHARS - guidance - reserve,
        )

    def _fit_journal(self, indexed: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
        """The prefix of the slice that fits :meth:`_journal_budget`.

        Always at least one entry, so an oversized line cannot stall the
        cursor; it is clipped when rendered.
        """
        budget = self._journal_budget()
        out: list[tuple[int, dict]] = []
        used = 0
        for n, e in indexed:
            cost = self._entry_cost(e)
            if out and used + cost > budget:
                break
            out.append((n, e))
            used += cost
        return out

    def _journal_lines(self, entries: list[dict]) -> list[str]:
        lines = ["## Session journal (conversation + executed cells, in order)"]
        clip = self._journal_budget()
        cell_no = 0
        current: tuple[Any, Any] | None = None
        for e in entries:
            tag = (e.get("session"), e.get("agent"))
            if any(tag) and tag != current:
                current = tag
                label = f"session {tag[0] or '?'}"
                if tag[1]:
                    label += f", agent {tag[1]}"
                lines.append(f"--- {label} ---")
            if e.get("kind", "cell") not in ("user", "assistant", "remember", "friction", "tool"):
                cell_no += 1
            rendered = "\n".join(self._render_entry(e, cell_no))
            if len(rendered) > clip:
                rendered = rendered[:clip] + " …[entry truncated]"
            lines.append(rendered)
        return lines

    def _guidance_lines(self) -> list[str]:
        if self._session_model is None:
            return []
        lines = ["", f"## Session model: {self._session_model}"]
        if self._guidance_snapshot:
            lines.append("Current model-guidance file(s) — a proposal replaces the whole file:")
            for stem, content in sorted(self._guidance_snapshot.items()):
                lines.append(f"### {stem}.md")
                lines.append(content)
        else:
            lines.append("(no model-guidance file exists for this model yet)")
        return lines

    def _prompt(self, entries: list[dict]) -> str:
        """The user message of one pass, at most ``PROMPT_CHAR_LIMIT`` chars
        for any journal slice :meth:`_fit_journal` admitted: the journal and
        the memory list have their own budgets and the skill library gets
        what is left. The model-guidance section goes last, so the only text
        a final safety cut could remove is guidance, never the journal."""
        memory = self._memory_lines()
        journal = self._journal_lines(entries)
        guidance = self._guidance_lines()
        fixed = len("\n".join(memory + [""] + journal + guidance)) + 2
        library = self._library_lines(entries, PROMPT_CHAR_LIMIT - fixed)
        return "\n".join(library + [""] + memory + [""] + journal + guidance)

    async def _complete(
        self, prompt: str, system: str = _SYNTH_SYSTEM,
    ) -> tuple[str, dict[str, int]]:
        """One plain completion: the text, and the tokens it cost.

        Usage arrives on ``message_start`` and ``usage`` chunks; a later chunk
        restates a counter (Anthropic's final ``output_tokens``), so the last
        value per counter wins within the call before it is added to the
        running totals. The stop reason rides the same chunks (or
        ``message_stop`` on chat-completions dialects); a ``max_tokens`` stop
        or an empty text channel raises :class:`SynthesisIncomplete`, and an
        error chunk (HTTP rejection, stream failure) raises
        :class:`SynthesisProviderError` carrying the provider's message — the
        model never reached a verdict, so the caller must not treat the
        result as NO_CHANGES.
        """
        if not self._store._facts.claim_inference_call():
            raise InferenceBudgetExhausted(
                "Daily inference-model budget reached; explicit memory saves remain available"
            )
        if len(prompt) > PROMPT_CHAR_LIMIT:
            # _prompt keeps the journal inside the limit; reaching this cut
            # means an oversized guidance file, and it is worth a line.
            _log.warning(
                "Memory learner prompt of %d chars cut to %d (model=%s)",
                len(prompt), PROMPT_CHAR_LIMIT, self._model,
            )
            prompt = prompt[:PROMPT_CHAR_LIMIT]
        request = ProviderRequest(
            model=self._model,
            messages=[Message(role="user", content=[Text(text=prompt)])],
            system=system,
            tools=[],
            max_tokens=self._max_tokens,
            effort=self._effort,
        )
        parts: list[str] = []
        raw: dict[str, Any] = {}
        stop_reason: str | None = None
        error: dict[str, Any] | None = None
        async for chunk in self._provider.stream(request):
            payload = chunk.payload or {}
            if chunk.kind == "text_delta":
                parts.append(payload["text"])
            elif chunk.kind in ("message_start", "usage", "message_stop"):
                reported = payload.get("usage")
                if isinstance(reported, dict):
                    raw.update({k: v for k, v in reported.items() if v})
                if payload.get("stop_reason"):
                    stop_reason = str(payload["stop_reason"])
            elif chunk.kind == "error" and error is None:
                error = dict(payload)
        usage: dict[str, int] = {}
        _add_usage(usage, raw)
        self.last_pass_usage = dict(usage)
        self.last_stop_reason = stop_reason
        _add_usage(self.usage, raw)
        if error is not None:
            raise SynthesisProviderError(error, usage)
        text = "".join(parts)
        if stop_reason == "max_tokens" or not text.strip():
            raise SynthesisIncomplete(stop_reason, usage, len(text))
        return text, usage

    async def _apply(self, text: str, entries: list[dict] | None = None) -> list[str]:
        """Parse the model's output, verify candidates, apply what passes.

        Candidates that fail static validation OR the verification kernel
        (import + ``_selftest``) are skipped and recorded on ``last_error`` —
        synthesized knowledge has to prove it works before it lands. Returns
        the names saved (or ``-name`` for deletions).

        ``entries`` is the journal slice the output came from: each memory is
        stamped with the provenance :func:`memory_provenance` derives from
        it, and the ones that only tool output supports are listed on
        ``last_held``. Without a slice (a direct call) provenance is left
        alone.
        """
        changed: list[str] = []
        errors: list[str] = []
        held: list[str] = []
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
        for memory_id, scope_attr, memory_text, problem in _memory_tags(text):
            if problem:
                errors.append(problem)
                continue
            provenance = (
                memory_provenance(memory_text, entries) if entries is not None else None
            )
            try:
                saved = self._store.save_memory(
                    memory_text.strip(),
                    memory_id=memory_id,
                    scope=self._resolve_scope(scope_attr),
                    provenance=provenance,
                )
            except ValueError as exc:
                errors.append(f"memory: {exc}")
                continue
            changed.append(f"memory:{saved.id}")
            if saved.provenance == "tool":
                held.append(f"{saved.id}: {saved.text}")
        self.last_held = held
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

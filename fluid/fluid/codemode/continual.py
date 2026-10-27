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

import ast
import asyncio
import json
import logging
import re
import time
import warnings
from collections.abc import Callable
from typing import Any, NamedTuple

from fluid.codemode.facts import MAX_INFERENCE_CALLS_PER_DAY
from fluid.codemode.skills import (
    JOURNAL_MAX_BYTES,
    LEARNER_SILENCE_ALARM_SECONDS,
    MAX_LOG_BYTES,
    MAX_LOG_ENTRIES,
    SkillStore,
    journal_lock,
    learner_silence_seconds,
    referenced_skills,
    trim_jsonl,
    validate_skill,
)
from fluid.codemode.verify import verify_sources
from fluid.messages.content import Message, Text
from fluid.providers.base import ProviderRequest, WireState
from fluid.providers.windows import context_window_for

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
Each memory must contain 1–500 characters: keep one durable fact per entry,
split independent facts, and omit transient detail rather than exceeding the
limit. A user's task-specific choice is not automatically a general habit;
retain its scope and exceptions. Facts do not grant permission for actions.

A [user] entry correcting HOW the agent works (model per job, delegation,
reviews, CI, deploys, process) is a standing-preference proposal, not a
memory: the whole current rule in the user's words, under a short snake_case
topic name (subagent_models, deploy). The owner confirms it.

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

Channel rule: a recurring mechanical failure of the MODEL itself (the kinds
listed under model guidance below) is not fixed by a memory (a memory tells the next session
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
- newer listed fact replaces older non-explicit (same subject, not temporary): <supersede-memory old="m3" new="m4"/>
- a memory listed as "explicit" was saved on the user's request: never
  rewrite or delete it, and do not add a new memory that repeats it
- standing preference: <preference topic="subagent_models">rule</preference>
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
- retire facts a newer fact superseded: when two entries describe the same
  thing at different times (hardware, versions, addresses, paths, an
  owner's machine), the newer one wins — delete the older entry, or rewrite
  it without the outdated detail. A fact that still cites hardware a later
  entry says was replaced is stale, not history worth keeping;
- delete entries that contradict each other (keep the better-evidenced one),
  restate stale-looking absolutes more carefully, and drop task narratives,
  one-off outcomes (a PR merged, a test passed once, a branch's setup) and
  anything the repository itself records — those never belonged in memory;
- an entry with zero recalls and old timestamps is a candidate for deletion,
  but low usage alone is not proof of uselessness — prefer merging over
  deleting when the fact still looks true;
- for skills: delete ones that are superseded by a better one; do NOT write
  new skills in this pass.

Entries marked "read-only" were saved at the user's request: never rewrite
or delete them, but use them as the newest truth when judging whether
another entry is stale. Entries marked "agent-saved" were kept by an agent on
its own judgment; curate them like any other. The listing is grouped by
topic so related facts sit together; a store too large for one pass is
reviewed a section at a time.

A small, sharp store beats a large, stale one — but do not destroy
information wholesale: most passes change a handful of entries at most.

Output format — nothing else:
- to rewrite a memory: <memory id="m3">the merged/corrected fact</memory>
  (add scope="project" when the fact is project-specific)
- to remove a memory: <delete-memory id="m3"/>
- newer listed fact replaces older non-explicit (same subject, not temporary): <supersede-memory old="m3" new="m4"/>
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
_SUPERSEDE_RE = re.compile(r'<supersede-memory old="([A-Za-z0-9_:.\-]{1,160})" new="([A-Za-z0-9_:.\-]{1,160})"\s*/>')
_GUIDANCE_RE = re.compile(
    r'<guidance stem="([^"]{1,64})">\n?(.*?)</guidance>', re.DOTALL
)
_PREFERENCE_RE = re.compile(
    r'<preference((?:\s+[a-z_]+="[^"]*")*)\s*>\n?(.*?)</preference>', re.DOTALL
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
    from fluid.codemode.journal import strip_reminders

    words = _content_words(text)
    if not words:
        return "session"
    # Journals written before reminders were stripped at append time still
    # carry the app's "# Memory" snapshot in user entries. Counting it would
    # label a re-proposed tool fact as something the user said.
    user_words = _content_words(" ".join(
        strip_reminders(str(e.get("text", "")))
        for e in entries if e.get("kind") in _USER_KINDS
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
    judged, so the caller must not advance the cursor (an empty completion is
    not a clean NO_CHANGES pass).
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
    rather than raising; this keeps the provider's reason instead of
    reporting the rejection as an empty completion.
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


BUDGET_SPENT_MESSAGE = (
    "Daily inference-model budget reached; explicit memory saves remain available"
)


# Journal lines one pass reads. Low-value lines (see low_value_entry) are
# consumed without being shown, so this bounds the read, not the prompt: the
# prompt is bounded by PROMPT_CHAR_LIMIT below. At 40 lines and 32k chars a
# busy worker's pass showed about 12 entries, 48 calls covered about 570 of
# roughly 1,700 a day, and the overflow reap dropped the rest unread (#103).
MAX_ENTRIES_PER_PASS = 400
# The request's user-message size. Split so the journal always fits: memory
# lines have a fixed budget, the skill library a reserve, and the journal the
# rest (never less than the minimum); the library then fills whatever the
# journal left unused. PROMPT_CHAR_LIMIT is the size for a learner model whose
# context window is unknown or small (a local model): with SYNTH_MAX_TOKENS of
# output it fits a 32k-token window. A known window raises it, at
# PROMPT_CHARS_PER_TOKEN characters per token left over after the output and
# the system prompt, up to LARGE_PROMPT_CHAR_LIMIT (about 25k input tokens),
# which is what lets a day's paced passes read a busy day's journal.
PROMPT_CHAR_LIMIT = 32_000
LARGE_PROMPT_CHAR_LIMIT = 96_000
PROMPT_CHARS_PER_TOKEN = 2
SYSTEM_PROMPT_TOKENS = 4_000
MEMORY_PROMPT_CHARS = 6_000
LIBRARY_PROMPT_RESERVE = 8_000
JOURNAL_MIN_PROMPT_CHARS = 8_000
# Consolidation lists the facts it may change (learned and agent-saved) and,
# read-only, the explicit ones they might contradict, grouped by topic. A
# store larger than this is reviewed a rotating section per pass.
CONSOLIDATE_MEMORY_CHARS = 24_000
# Rough cost of a "--- session ... ---" divider when the previous entry's
# session is unknown (see Synthesizer._entry_cost).
_SESSION_LABEL_ALLOWANCE = 80
# Slack the journal fit leaves for section headers and the memory list's
# "older facts not listed" line, which their budgets do not count.
_PROMPT_HEADROOM = 400
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
# How often the yield alarm repeats while the learner stays silent.
YIELD_ALARM_REPEAT_SECONDS = 86400.0
# A pass costs one of the day's 48 inference calls (facts.MAX_INFERENCE_CALLS_
# PER_DAY), so it has to see a real slice: a turn writes its user prompt, its
# assistant reply and a handful of cells, so 20 is roughly three turns of
# evidence.
MIN_NEW_ENTRIES = 20
# Output room for a pass, large enough that reasoning alone cannot consume
# it. Overridable through the constructor (``max_tokens``) for hosts that pin a smaller model.
SYNTH_MAX_TOKENS = 16_000
# Reasoning effort sent on every synthesis request. Low: the task is a
# read-and-classify over a bounded journal, and the budget is calls per day,
# not quality per call. Serializers map it per provider (Anthropic
# output_config.effort, OpenAI-shaped reasoning.effort).
SYNTH_EFFORT = "low"
# Skill source shown per pass: skills the slice referenced are always shown
# whole; the rest fill this budget most-used first and beyond it appear as an
# index line only.
SKILL_SOURCE_BUDGET = 24_000
# Lock file (next to the journal) held for the whole pass, so two processes
# over one store never pay for the same slice or write the same memory twice.
SYNTHESIS_LOCK = "synthesis"
# Consolidation (a whole-store curation pass) is interval-gated like skill
# curation; it rides an ordinary background pass, never session close. One
# store took about a dozen saves a day, so a weekly pass that saw one
# section of it never caught up.
CONSOLIDATE_INTERVAL_DAYS = 2.0
CONSOLIDATE_MIN_MEMORIES = 8
# Daily inference calls synthesis leaves for consolidation. Synthesis can
# otherwise spend the whole facts.MAX_INFERENCE_CALLS_PER_DAY budget, and a
# store whose learner is always out of budget is never curated. Both are
# defaults; see learner_budget for the configured ones.
CONSOLIDATE_RESERVED_CALLS = 2
# Synthesis calls are spread over the UTC day instead of spent as soon as
# entries arrive: by a given time of day a store may have made
# PACE_BURST_CALLS plus its share of the day's calls so far, and a pass
# starts at least MIN_PASS_INTERVAL after the previous call (less with a
# bigger configured budget, see min_pass_interval). A pass makes one
# call per project in its slice, back to back, and never more than the day's
# remaining calls (see _fit_calls). Before this, a busy worker spent all 48
# calls by 00:04-03:55 UTC and its learner read nothing for the rest of the
# day. A pass that waits reads a bigger slice instead. A pending Remember
# entry (mark_urgent) is not paced until it is read; the daily cap holds.
PACE_BURST_CALLS = 2
MIN_PASS_INTERVAL = 15 * 60.0
# Past this share of JOURNAL_MAX_BYTES (between the reap target and the
# overflow line) a pass may run ahead of the pace, still MIN_PASS_INTERVAL
# apart, so a burst of sessions is read instead of reaped.
PACE_PRESSURE_FRACTION = 0.6
# Successful cells are shown with their code and output clipped to these
# sizes; a failed cell is shown whole. What a working cell did is in its
# first lines and its tool list, and the long tail of a file it printed is
# what the repository records anyway.
OK_CELL_CODE_CHARS = 1_200
OK_CELL_OUTPUT_CHARS = 400
# The same for a successful JSON-surface tool call (one journal line per
# call; delegated sessions write nothing else between turns). On a busy
# worker 976 tool lines averaged 581 rendered chars and held 86% of a
# 48-minute journal's text, so a pass's slice ended a few turns in.
OK_TOOL_INPUT_CHARS = 300
OK_TOOL_OUTPUT_CHARS = 200
# The window a learner on a flat-rate subscription (ChatGPT/Codex,
# SuperGrok) is sized for when neither the host nor fluid's table knows its
# model: those are hosted models with far larger windows, and the small
# default (meant for a local model) cut the live learner's passes to about
# 37 entries while some 360 waited behind them.
SUBSCRIPTION_ASSUMED_WINDOW = 128_000
# Tools whose successful calls only read: file and search reads and task
# listings (MCP-namespaced ids match on their last segment). TaskGet and
# TaskOutput are not here: they carry a worker's findings.
READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead",
    "TaskList", "ToolSearch",
})


# The daily call budget follows how the learner's provider bills a pass: a
# flat-rate subscription (ChatGPT/Codex, SuperGrok) gets
# SUBSCRIPTION_DAILY_CALLS, anything metered or unknown keeps
# facts.MAX_INFERENCE_CALLS_PER_DAY (48), so nobody gets a surprise bill. An
# explicit setting (the app's owner policy, read at pass time, or fluid's
# learner_daily_calls) overrides either, clamped to these bounds. The
# consolidation reserve and the pacing scale with the effective budget. The
# decision is per learner, from the provider its passes use: one store can be
# served by both kinds, and each pass claims against its own limit.
SUBSCRIPTION_DAILY_CALLS = 200
LEARNER_DAILY_CALLS_MIN = 4
LEARNER_DAILY_CALLS_MAX = 1000
LEARNER_RESERVED_CALLS_MAX = 10
# MIN_PASS_INTERVAL belongs to the default budget's synthesis share; a bigger
# budget shortens it in proportion, never below this.
MIN_PASS_INTERVAL_FLOOR = 60.0
# How long a learner reuses its resolved budget between passes (due() runs
# on every journaled cell); every pass resolves it afresh.
BUDGET_REFRESH_SECONDS = 60.0
BILLING_SUBSCRIPTION = "subscription"
BILLING_METERED = "metered"


class LearnerBudget(NamedTuple):
    """A learner's effective daily budget and where it came from:
    ``setting``, ``subscription_default`` or ``metered_default``."""

    daily: int
    reserved: int
    source: str

    def record(self) -> dict[str, Any]:
        return {"daily": self.daily, "reserved": self.reserved, "source": self.source}


def _bounded(value: Any, low: int, high: int) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def consolidation_reserve(daily: int) -> int:
    """Calls of ``daily`` kept for consolidation: CONSOLIDATE_RESERVED_CALLS
    at the default budget, in proportion otherwise (at least 1, at most
    LEARNER_RESERVED_CALLS_MAX, never more than half the day)."""
    share = round(daily * CONSOLIDATE_RESERVED_CALLS / max(1, MAX_INFERENCE_CALLS_PER_DAY))
    return min(daily // 2, max(1, min(LEARNER_RESERVED_CALLS_MAX, share)))


def learner_budget(setting: Any = None, billing: str | None = None) -> LearnerBudget:
    """The effective budget for an explicit ``setting`` (calls a day; None,
    0 or a non-number means automatic) and the pass's ``billing``."""
    daily = None if setting in (0, "0") else _bounded(setting, LEARNER_DAILY_CALLS_MIN,
                                                      LEARNER_DAILY_CALLS_MAX)
    if daily is not None:
        source = "setting"
    elif billing == BILLING_SUBSCRIPTION:
        daily, source = SUBSCRIPTION_DAILY_CALLS, "subscription_default"
    else:
        daily, source = MAX_INFERENCE_CALLS_PER_DAY, "metered_default"
    return LearnerBudget(daily, consolidation_reserve(daily), source)


def paced_call_allowance(now: float, budget: LearnerBudget | None = None) -> int:
    """Synthesis calls a store may have made by ``now`` in its UTC day."""
    budget = budget or learner_budget()
    share = budget.daily - budget.reserved
    elapsed = (now % 86400) / 86400
    return min(share, PACE_BURST_CALLS + int(share * elapsed))


def min_pass_interval(budget: LearnerBudget | None = None) -> float:
    """Seconds a pass waits after the previous call: MIN_PASS_INTERVAL at
    the default budget or below, shorter in proportion above it."""
    budget = budget or learner_budget()
    default_share = MAX_INFERENCE_CALLS_PER_DAY - CONSOLIDATE_RESERVED_CALLS
    share = max(1, budget.daily - budget.reserved)
    if share <= default_share:
        return MIN_PASS_INTERVAL
    return max(MIN_PASS_INTERVAL_FLOOR, MIN_PASS_INTERVAL * default_share / share)


def _clip(text: str, limit: int) -> str:
    """``text`` cut to about ``limit`` chars, keeping its head and its tail:
    a test summary, a push result or a printed error sits at the end."""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]} …[{len(text) - limit} chars clipped]… {text[-tail:]}"


def prompt_char_limit(context_window: int | None, max_tokens: int = 16_000) -> int:
    """The learner's request size for a model with ``context_window`` tokens."""
    if not context_window or context_window <= 0:
        return PROMPT_CHAR_LIMIT
    room = (context_window - max_tokens - SYSTEM_PROMPT_TOKENS) * PROMPT_CHARS_PER_TOKEN
    return max(PROMPT_CHAR_LIMIT, min(LARGE_PROMPT_CHAR_LIMIT, room))


def _tool_base(name: Any) -> str:
    return str(name or "").rsplit("__", 1)[-1]


def low_value_entry(entry: dict) -> bool:
    """True for a journal entry the learner consumes without showing it.

    A call or cell that succeeded and only read (``READ_ONLY_TOOLS``) shows
    repository content or task progress: what the repository or the task
    already records, which the learner is told never to memorize. Everything
    else is shown: conversation, Remember and friction entries, any failure,
    every cell that wrote, ran a command, used a skill or called no tool, and
    any read that named a path outside the session's working directory and
    the temp directory (``outside_cwd``, stamped at append): home config and
    notes carry facts.
    """
    kind = entry.get("kind", "cell")
    if kind == "tool":
        return (not entry.get("error") and not entry.get("outside_cwd")
                and _tool_base(entry.get("name")) in READ_ONLY_TOOLS)
    if kind != "cell" or entry.get("error"):
        return False
    if entry.get("outside_cwd"):
        return False
    calls = entry.get("calls")
    if isinstance(calls, list) and any(
        isinstance(c, dict) and c.get("ok") is False for c in calls
    ):
        return False
    tools = entry.get("tools") or []
    if not tools or not all(_tool_base(t) in READ_ONLY_TOOLS for t in tools):
        return False
    code = str(entry.get("code", ""))
    if referenced_skills(code):
        return False
    # Reading data does not make the accompanying computation disposable.
    # Preserve reusable definitions for skill synthesis, without executing any
    # journal code. An unparseable cell is uncertain, not safe to discard.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            if any(isinstance(target, ast.Name) for target in node.targets):
                return False
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and isinstance(node.value, ast.Lambda)):
            return False
    return True


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
        context_window: int | None = None,
        workflow_cwd: str | None = None,
        daily_calls: int | Callable[[], Any] | None = None,
        billing: str | Callable[[], str | None] | None = None,
    ) -> None:
        self._workflow_cwd = workflow_cwd
        # The daily call budget this learner spends (see learner_budget):
        # ``daily_calls`` is the explicit setting, a number or a callable
        # read at pass time (None or 0: automatic); ``billing`` says how this
        # learner's provider bills a pass ("subscription", "metered"; None is
        # unknown, treated as metered), also a value or a callable.
        self._daily_calls = daily_calls
        self._billing = billing
        self._resolved_budget: LearnerBudget | None = None
        self._budget_at = float("-inf")
        self._provider = provider
        self._model = model
        self._store = store
        # The current project's memory-scope key: ``scope="project"`` tags in
        # the model's output resolve to it. ``None`` keeps everything global.
        self._project = project
        # The SESSION's model id (may differ from the synthesis
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
        # The request size this learner model can take: ``context_window``
        # from the host when it knows the model's window, else fluid's
        # table, else the small default (see prompt_char_limit).
        self._window = context_window or context_window_for(model)
        self._prompt_limit = prompt_char_limit(self._window, max_tokens)
        # The limit the window gave; a pass re-derives it while it is still
        # in force (see _size_request), never over a value set on purpose.
        self._window_limit = self._prompt_limit
        self._effort = effort
        self._task: asyncio.Task | None = None
        # UTC day (time // 86400) on which the daily budget was found spent.
        # Until the day rolls over ``due()`` is False, so a kick per cell
        # does not append hundreds of identical budget rows to the pass log.
        self._budget_spent_day: int | None = None
        # The budget in force when the day was found spent, and whether the
        # latch was checked against the budget as last resolved (see
        # _budget_raised).
        self._latched_budget: LearnerBudget | None = None
        self._latch_checked = False
        # The last completion's stop reason, for the pass log.
        self.last_stop_reason: str | None = None
        # A pass is one model call plus verification kernels; bound it so a
        # stalled stream can never wedge a turn or, worse, session close.
        self._pass_timeout = pass_timeout
        self._close_timeout = close_timeout
        # Set by mark_urgent(): lowers the trigger threshold to "any new
        # entries at all", so the request lands on the next pass instead of
        # waiting for MIN_NEW_ENTRIES.
        self._urgent = False
        # The journal line count when urgency was raised (see mark_urgent).
        self._urgent_line = 0
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
        # True while the when-line backfill runs after a pass, so a close
        # that times out then is charged to the backfill, not to synthesis.
        self._in_backfill = False
        # When this synthesizer first found the synthesis lock held, and when
        # it last wrote that down (see run()).
        self._lock_busy_since: float | None = None
        self._lock_logged_at = float("-inf")
        self._kick_warned = False
        # This replica's store id, for log lines (see _store_label).
        self._store_id: str | None = None
        # Request routing for this learner's calls, bound by the loop that
        # owns the session (see bind_routing). None sends no routing at all.
        self._routing: Callable[[], tuple[dict[str, Any], WireState | None]] | None = None

    def bind_routing(
        self, routing: Callable[[], tuple[dict[str, Any], WireState | None]],
    ) -> bool:
        """Route this learner's calls with ``routing``; True when bound.

        ``routing`` returns the request ``extra`` (the cache-affinity key)
        and the loop-owned ``WireState`` for the learner role. The first
        binder wins: a worker loop that shares its parent's synthesizer must
        not move the learner's key onto the worker's session.
        """
        if self._routing is not None:
            return False
        self._routing = routing
        return True

    def _request_routing(self) -> tuple[dict[str, Any], WireState | None]:
        # The routing speaks the Responses client's dialect; a learner pinned
        # to another client sends none of it.
        if self._routing is None or getattr(self._provider, "name", None) != "openai_responses":
            return {}, None
        try:
            extra, wire = self._routing()
        except Exception:  # noqa: BLE001 — routing never fails a pass
            _log.debug("learner routing lookup failed", exc_info=True)
            return {}, None
        return dict(extra or {}), wire

    # ─── triggering ──────────────────────────────────────────────────────
    def mark_urgent(self) -> None:
        """A high-salience entry (Remember) is waiting: synthesize ASAP.

        Call after journaling the entry. Urgency lasts until a pass has read
        every Remember entry, not until the whole backlog is read: a Remember
        must not drain a paced backlog in back-to-back calls."""
        self._urgent = True
        try:
            self._urgent_line = max(self._urgent_line, self._store.journal_count())
        except Exception:  # noqa: BLE001 — unknown: the next pass clears it
            pass

    def due(self, *, closing: bool = False) -> bool:
        if self._budget_spent_day is not None:
            if (int(time.time() // 86400) == self._budget_spent_day
                    and not self._budget_raised()):
                return False
            self._budget_spent_day = None
            self._latch_checked = False
        try:
            backoff = self._store.learner_backoff(self._model)
        except Exception:
            backoff = {}
        if backoff and time.time() < float(backoff.get("until", 0.0)):
            return False
        new = self._store.journal_count() - self._store.synth_cursor()
        if new < 1:
            return False
        # Urgency bypasses pacing, not the shared daily cap. A fresh engine
        # must not repeatedly select a pass that every other engine knows
        # cannot claim a call. The atomic claim remains authoritative.
        if self._urgent:
            left = self._calls_left()
            return left is None or left > 0
        # Non-urgent pacing already reads and enforces the shared cap.
        # Session end is a batch boundary, not a pacing/budget bypass.
        return self._paced() and (new >= self._min_new if closing else self._batch_ready())

    def _batch_ready(self) -> bool:
        """Batch substantive evidence, never discard it or alter force-run."""
        indexed, _ = self._store.journal_slice_indexed(self._store.synth_cursor(), self._max_entries)
        if not indexed:
            return False
        shown = [entry for _, entry in indexed if not low_value_entry(entry)]
        if not shown:
            return len(indexed) >= self._min_new  # drain read-only slices free
        if self._waits_for_conversation(indexed, [(0, entry) for entry in shown]):
            return False
        now = time.time()
        # Missing legacy timestamps are uncertain: consume rather than starve.
        oldest = min(entry.get("ts") if type(entry.get("ts")) in (int, float) else 0 for entry in shown)
        signal = any(entry.get("kind") in {"user", "remember", "friction"} or entry.get("error")
                     or any(isinstance(call, dict) and call.get("ok") is False
                            for call in (entry["calls"] if isinstance(entry.get("calls"), list) else []))
                     for entry in shown)
        if now - oldest >= 3600 and (len(shown) >= 3 or signal):
            return True
        if len(indexed) < self._min_new:
            return False
        if (len(shown) >= self._min_new or len(indexed) >= self._max_entries
                or self._store._journal_size() > JOURNAL_MAX_BYTES * PACE_PRESSURE_FRACTION):
            return True
        return signal

    def _budget(self, *, refresh: bool = False) -> LearnerBudget:
        """This learner's effective budget. Resolved afresh for every pass
        (``refresh``) and otherwise at most every BUDGET_REFRESH_SECONDS, so
        a changed setting applies without a restart and ``due()`` (run per
        journaled cell) does not read it each time. Never raises: a setting
        or billing that cannot be read counts as unset or unknown."""
        now = time.time()
        if (refresh or self._resolved_budget is None
                or now < self._budget_at
                or now - self._budget_at >= BUDGET_REFRESH_SECONDS):
            setting = self._resolve(self._daily_calls)
            billing = self._resolve(self._billing)
            self._resolved_budget = learner_budget(
                setting, billing if isinstance(billing, str) else None,
            )
            self._budget_at = now
        return self._resolved_budget

    def _size_request(self) -> None:
        """Size this pass's request. With no known window a learner billed
        as a subscription is a hosted model: it gets
        SUBSCRIPTION_ASSUMED_WINDOW instead of the small local-model
        default. A limit set on purpose (not the one the window gave) is
        left alone."""
        if self._window or self._prompt_limit != self._window_limit:
            return
        billing = self._resolve(self._billing)
        window = SUBSCRIPTION_ASSUMED_WINDOW if billing == BILLING_SUBSCRIPTION else None
        self._prompt_limit = self._window_limit = prompt_char_limit(window, self._max_tokens)

    def _journal_pressure(self) -> bool:
        """True when the journal is heading for the overflow reap (past
        PACE_PRESSURE_FRACTION of its cap): reading beats waiting."""
        try:
            return self._store._journal_size() > JOURNAL_MAX_BYTES * PACE_PRESSURE_FRACTION
        except Exception:  # noqa: BLE001 — unknown size: do not hold a pass back
            return True

    def _waits_for_conversation(self, indexed: list, shown: list) -> bool:
        """Whether a pass over this slice should wait instead of calling.

        A slice of successful tool calls only (no conversation entry, no
        friction, no failure, no code-mode cell) shows work without the
        request behind it or the user's reaction to it: on the live store
        976 of a 48-minute journal's 1,137 lines were such calls, and the
        passes over them came back NO_CHANGES. It waits until a turn's
        prompt or reply lands, unless waiting cannot help: the read is
        already full (the next conversation line is past it), or the journal
        is near the overflow reap and would drop the slice unread. Cells
        still run on their own: they are what skills are distilled from,
        and friction and failures are evidence by themselves."""
        if not shown or any(
            entry.get("kind") != "tool" or entry.get("error") for _, entry in shown
        ):
            return False
        if len(indexed) >= self._max_entries:
            return False
        return not self._journal_pressure()

    def _latch_spent_day(self) -> None:
        """A claim was refused: wait quietly until the UTC day rolls over
        or the budget is raised (see due())."""
        self._budget_spent_day = int(time.time() // 86400)
        self._latched_budget = self._resolved_budget
        self._latch_checked = False

    def _budget_raised(self) -> bool:
        """Whether a budget found spent today has room again: the setting
        was raised (or the pass's billing changed) since, and the store's
        calls today are under the new cap. Re-checked only when the budget
        is re-resolved (at most every BUDGET_REFRESH_SECONDS), so a latched
        ``due()`` stays cheap."""
        resolved_at = self._budget_at
        effective = self._budget()
        if self._budget_at == resolved_at and self._latch_checked:
            return False
        self._latch_checked = True
        latched = self._latched_budget
        if latched is not None and effective.daily - effective.reserved <= (
                latched.daily - latched.reserved):
            return False
        try:
            spent = self._store._facts.inference_budget()
        except Exception:  # noqa: BLE001 — unreadable: keep waiting, the claim decides
            return False
        calls = int(spent.get("calls") or 0) if spent.get("day") == self._budget_spent_day else 0
        # The cap the next claim is checked against: the metered default
        # claims against the fact store's own (see _claim_limit).
        from fluid.codemode import facts as facts_mod
        cap = self._claim_limit() or facts_mod.MAX_INFERENCE_CALLS_PER_DAY
        return calls < cap - effective.reserved

    @staticmethod
    def _resolve(value: Any) -> Any:
        if not callable(value):
            return value
        try:
            return value()
        except Exception:  # noqa: BLE001 — unreadable is unset, never a failed pass
            _log.debug("learner budget input unreadable", exc_info=True)
            return None

    def _claim_limit(self) -> int | None:
        """The daily cap to claim against; None lets the fact store use its
        built-in one (the metered default is that cap)."""
        budget = self._budget()
        return None if budget.source == "metered_default" else budget.daily

    def _paced(self, now: float | None = None) -> bool:
        """True when the day's spread of calls leaves room for a pass now
        (see PACE_BURST_CALLS). A pass held back here leaves no log row."""
        now = time.time() if now is None else now
        try:
            budget = self._store._facts.inference_budget()
        except Exception:  # noqa: BLE001 — an unreadable budget is checked at claim
            return True
        if budget.get("day") != int(now // 86400):
            return True
        calls = int(budget.get("calls") or 0)
        effective = self._budget()
        if calls >= effective.daily - effective.reserved:
            return False  # spent: every session's learner waits for the next day quietly
        if now - float(budget.get("last_at") or 0.0) < min_pass_interval(effective):
            return False
        if calls < paced_call_allowance(now, effective):
            return True
        # Ahead of the pace, but the backlog is heading for the overflow
        # reap: a later call spent now beats entries dropped unread.
        return self._store._journal_size() > JOURNAL_MAX_BYTES * PACE_PRESSURE_FRACTION

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
                    "Memory learner (store=%s): a pass is due but kick() was "
                    "called with no running event loop; synthesis will wait "
                    "for the next kick from the loop thread or session close",
                    self._store_label(),
                )
            return
        self._task = loop.create_task(self._run_safe())

    async def drain(self) -> None:
        """Await any in-flight pass, then run a final one if entries remain.

        Session end bypasses signal batching, not the base entry threshold,
        pacing, shared budget or failure backoff. Short slices remain on disk.
        """
        try:
            await asyncio.wait_for(self._drain_inner(), timeout=self._close_timeout)
        except TimeoutError:
            if self._in_backfill:
                # Synthesis finished; the when-line backfill after it ran out
                # of the close budget synthesis shares with it. That says
                # nothing about the provider: no backoff, no failure count,
                # no failed synthesis pass. Its lines stay missing and the
                # next session's backfill picks them up.
                self._in_backfill = False
                _log.info("when-line backfill on close was cut off after %.0fs; "
                          "it resumes next session", self._close_timeout)
            else:
                self.last_error = (
                    f"synthesis on close exceeded {self._close_timeout:.0f}s and was abandoned"
                )
                # An abandoned pass leaves no cursor movement (the slice
                # retries next session) — but it must leave a trace, or a
                # slow pass is indistinguishable from one that never ran.
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
        if self.due(closing=True):
            await self._run_safe()

    async def _run_safe(self) -> list[str]:
        self._in_backfill = False
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
            self._latch_spent_day()
            self.last_error = f"{type(exc).__name__}: {exc}"
            # No call was made, so this is not a silent pass. One row a day
            # per store says why the learner is idle; every session that
            # reached the cap used to add its own (324 of a live log's 648).
            self._log_budget_spent("synthesis")
            # Synthesis stops short of CONSOLIDATE_RESERVED_CALLS, so a due
            # curation pass can still run on the calls it left.
            changed = []
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
            except InferenceBudgetExhausted:
                self._latch_spent_day()
                self._log_budget_spent("consolidation")
            except Exception as exc:  # consolidation is opportunistic, never fatal
                self._consolidation_failed(f"{type(exc).__name__}: {exc}")
        if not (self.last_pass_failed or self.last_pass_skipped):
            # Best-effort and self-contained: its failures (including its own
            # timeout) go to the backfill's backoff, never last_pass_failed.
            from fluid.codemode.memory_backfill import run as backfill_when
            # Left set if a close cancels it, so drain() can tell (a finally
            # would clear it before drain sees the timeout).
            self._in_backfill = True
            backfilled = await backfill_when(self)
            self._in_backfill = False
            changed += ["memory-when:" + mid for mid in backfilled.lines]
        await self._notify(changed)
        return changed

    def _log_budget_spent(self, kind: str) -> None:
        """The day's single "budget spent" row for this store (``ran``
        False, so memory health never counts it as a failed pass)."""
        try:
            first = self._store.claim_budget_row(int(time.time() // 86400))
        except Exception:  # noqa: BLE001 — unknown: one more row beats none
            first = True
        if first:
            self._log_pass(
                kind, [], error=f"{InferenceBudgetExhausted.__name__}: {BUDGET_SPENT_MESSAGE}",
                ran=False,
            )

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
        self._budget(refresh=True)  # a changed setting applies from this pass on
        self._size_request()
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
            ran=False,
        )

    async def _run_locked(self) -> list[str]:
        forgotten_before = self._store._facts.snapshot().get("forgotten_before", 0)
        # The slice is leased while the pass waits for the model: a reap in
        # that time leaves it alone and reports how far it moved, so the
        # cursor lands on the line this pass reached, not on a position that
        # now holds a line it never read (see SkillStore.begin_synth_read).
        lease, cursor, indexed, reached = self._store.begin_synth_read(self._max_entries)
        try:
            return await self._run_slice(lease, forgotten_before, cursor, indexed, reached)
        finally:
            self._store.end_synth_read(lease)

    async def _run_slice(
        self, lease: str | None, forgotten_before: float, cursor: int,
        indexed: list[tuple[int, dict]], reached: int,
    ) -> list[str]:
        slice_end = reached
        indexed = [(n, e) for n, e in indexed if e.get("ts", 0) > forgotten_before]
        if not indexed:
            if reached > cursor:
                # Only malformed lines in the slice: step past them, or every
                # later pass would stop at the same corrupt line.
                self._store.commit_synth_cursor(lease, reached)
            return []
        # Low-value entries are consumed unshown, so neither the prompt nor
        # the day's calls are spent on them.
        shown = [(n, e) for n, e in indexed if not low_value_entry(e)]
        if not shown:
            self._store.commit_synth_cursor(lease, reached)
            self._log_pass("synthesis", [], consumed=0, filtered=len(indexed), ran=False)
            try:
                self._store.reap_journal()
            except Exception:
                pass
            return []
        if self._waits_for_conversation(indexed, shown):
            # Not a verdict: nothing consumed, no call, no row (like a pass
            # pacing holds back). Session close and urgency land here too.
            self.last_pass_skipped = True
            return []
        # Only what the prompt can show is consumed: the rest of the slice
        # stays behind the cursor for the next pass. Each project in the
        # slice costs one call, so the slice also stops before a project the
        # day's remaining calls could not pay for.
        fitted = self._fit_calls(self._fit_journal(shown))
        deferred = len(shown) - len(fitted)
        if deferred:
            reached = fitted[-1][0]
        groups: dict[str | None, list[dict]] = {}
        for _, entry in fitted:
            groups.setdefault(entry.get("project"), []).append(entry)
        changed: list[str] = []
        usage: dict[str, int] = {}
        text = ""
        done: set[str | None] = set()
        stop: Exception | None = None
        original_project = self._project
        try:
            for project, group in groups.items():
                # Unattributed legacy input may produce global facts, but must
                # not be assigned to whichever project happens to run next.
                self._project = project or "legacy:unassigned"
                try:
                    prompt, system, workflow_entries = self._synthesis_input(group)
                    text, group_usage = await self._complete(prompt, system)
                except (SynthesisIncomplete, SynthesisProviderError) as exc:
                    for key, value in exc.usage.items():
                        usage[key] = usage.get(key, 0) + value
                    stop = exc
                    break
                except InferenceBudgetExhausted as exc:
                    if not done:
                        raise  # no call made: _run_safe logs the skip
                    stop = exc
                    break
                for key, value in group_usage.items():
                    usage[key] = usage.get(key, 0) + value
                if self._store._facts.snapshot().get("forgotten_before", 0) > forgotten_before:
                    return []
                changed.extend(await self._apply(text, group, workflow_entries=workflow_entries))
                done.add(project)
        finally:
            self._project = original_project
        if stop is not None and not done:
            # Not a verdict on the slice: leave the cursor where it is so the
            # next pass retries it (after the backoff), and log what came back
            # so a rejected or empty stream is never mistaken for NO_CHANGES.
            self.last_error = str(stop)
            retry = self._note_failure(self.last_error)
            self._log_pass(
                "synthesis", changed, error=self.last_error,
                consumed=0, output_chars=getattr(stop, "output_chars", None),
                usage=usage, stop_reason=getattr(stop, "stop_reason", None),
                provider_status=getattr(stop, "status", None),
                retry_after_s=retry,
            )
            return changed
        if stop is not None:
            # Some projects were answered and applied: consume the prefix of
            # the slice they cover, so those entries are not paid for again.
            prefix = []
            for n, entry in fitted:
                if entry.get("project") not in done:
                    break
                prefix.append((n, entry))
            reached = prefix[-1][0] if prefix else cursor
            deferred = len(shown) - len(prefix)
            fitted = prefix
        entries = [e for _, e in fitted]
        filtered = sum(1 for n, _ in indexed if n <= reached) - len(fitted)
        if self._store._facts.snapshot().get("forgotten_before", 0) <= forgotten_before:
            # Translated over any reap during the call, and clamped: a cursor
            # past the end would make pending negative and stop the learner
            # until the journal caught up.
            self._store.commit_synth_cursor(lease, reached)
        # Otherwise a user Forget landed while the pass ran: its purge already
        # rebased the cursor over a rewritten journal, so ``reached`` no
        # longer indexes the same lines and the cursor is left alone. The
        # pass itself still succeeded and is logged below.
        retry = None
        if isinstance(stop, InferenceBudgetExhausted):
            self._latch_spent_day()
            self.last_error = f"{type(stop).__name__}: {stop}"
        elif stop is not None:
            self.last_error = str(stop)
            retry = self._note_failure(self.last_error)
        else:
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
        # Urgency lasts while a Remember entry is still unread: one this
        # slice holds past the reach, or the line urgency was raised at when
        # the slice did not get that far. The rest of the backlog is paced.
        if self._urgent and self._urgent_line <= slice_end and not any(
            e.get("kind") == "remember" for n, e in indexed if n > reached
        ):
            self._urgent = False
        self._log_pass(
            "synthesis", changed, error=self.last_error,
            consumed=len(entries), output_preview=text, usage=usage,
            stop_reason=self.last_stop_reason, deferred=deferred or None,
            filtered=filtered or None, retry_after_s=retry,
        )
        return changed

    def _calls_left(self) -> int | None:
        """Synthesis calls left in the store's day, or None when unknown."""
        try:
            budget = self._store._facts.inference_budget()
        except Exception:  # noqa: BLE001 — the claim still enforces the cap
            return None
        calls = int(budget.get("calls") or 0) if budget.get("day") == int(time.time() // 86400) else 0
        effective = self._budget()
        return effective.daily - effective.reserved - calls

    def _fit_calls(self, fitted: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
        """The prefix of ``fitted`` whose projects the day's remaining calls
        can pay for (always the first project, which the claim then decides)."""
        left = self._calls_left()
        if left is None:
            return fitted
        projects: set[str | None] = set()
        for i, (_, entry) in enumerate(fitted):
            project = entry.get("project")
            if project not in projects:
                if projects and len(projects) >= max(1, left):
                    return fitted[:i]
                projects.add(project)
        return fitted

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
        filtered: int | None = None,
        ran: bool = True,
    ) -> None:
        """Append one pass record to ``.synthesis-log.jsonl`` (best-effort).

        ``ran=False`` marks a row for a pass that made no model call (the
        lock was held elsewhere, the daily budget was spent, or every entry
        was low-value); it carries ``"ran": false`` and does not count toward
        the yield alarm. ``filtered`` counts low-value entries the pass
        consumed without showing them.

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
        if self._resolved_budget is not None:
            # Which daily budget applied, and why (setting, or the default
            # for how the pass's provider bills it).
            record["budget"] = self._resolved_budget.record()
        store = self._store_label()
        # Replicas merge memories.json but each keeps its own log; the id
        # says which learner wrote a row once logs are compared.
        record["store_id"] = store
        if consumed is not None:
            record["consumed"] = consumed
        if deferred:
            record["deferred"] = int(deferred)
        if filtered:
            record["filtered"] = int(filtered)
        if not ran:
            record["ran"] = False
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
                "Memory learner %s pass (model=%s, store=%s): consumed %s, changed %s, rejected: %s",
                kind, self._model, store, consumed, changed or "nothing", error,
            )
        elif error:
            _log.warning(
                "Memory learner %s pass failed (model=%s, store=%s, root=%s): %s%s",
                kind, self._model, store, self._store.root, error,
                f"; next attempt in {retry_after_s:.0f}s" if retry_after_s else "",
            )
        else:
            _log.info(
                "Memory learner %s pass (model=%s, store=%s): consumed %s, changed %s",
                kind, self._model, store, consumed, changed or "nothing",
            )
        if ran:
            self._track_yield(changed, store)
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

    def _store_label(self) -> str:
        """This replica's store id for log lines ("new" until memories.json
        exists; the id is fixed once it does, so it is read once)."""
        if self._store_id is None:
            try:
                self._store_id = self._store.store_id()
            except Exception:
                self._store_id = None
        return self._store_id or "new"

    def _track_yield(self, changed: list[str], store: str) -> None:
        """Count a pass toward the store's yield record and warn when the
        learner has run passes for ``LEARNER_SILENCE_ALARM_SECONDS`` without
        writing anything (once a day while the silence lasts). Deletions do
        not count as a write. Best-effort, never raises."""
        now = time.time()
        wrote = any(not name.startswith("-") for name in changed)
        try:
            record = self._store.note_learner_pass(wrote, now=now)
            silent = learner_silence_seconds(record, now)
            if silent < LEARNER_SILENCE_ALARM_SECONDS:
                return
            if now - float(record.get("alarm_at", 0) or 0) < YIELD_ALARM_REPEAT_SECONDS:
                return
            last = record.get("last_write_at")
            _log.warning(
                "Memory learner yield alarm (store=%s, model=%s): no fact written "
                "in %.1f days across %d passes (last write: %s). Recent pass "
                "outcomes are in %s",
                store, self._model, silent / 86400, int(record.get("silent_passes", 0)),
                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(last))) if last else "never",
                self._store.root / ".synthesis-log.jsonl",
            )
            self._store.note_learner_yield_alarm(now)
        except Exception:
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
        # Model-free: two user-kept facts on one how-the-agent-works topic
        # where the newer changes the rule become an owner question (both
        # kept; the snapshot shows the conflict line). The model below may
        # not touch user-kept facts at all.
        from fluid.codemode.preferences import curate_protected_conflicts
        curated = curate_protected_conflicts(self._store)
        lines = ["## Memory store (id, recalls, age, scope, tier, text), grouped by topic"]
        now = time.time()
        everything = [m for m in self._store.memories() if not m.conflicted]
        # Grouped by scope and key/topic so a fact sits beside the facts that
        # could supersede it, and read from a rotating offset so a store too
        # large for one pass is reviewed in full over successive passes
        # instead of only its newest entries every time.
        everything.sort(key=lambda m: (m.scope, (m.key or "~").split(".")[0].casefold(),
                                       (m.key or "").casefold(), m.text.casefold(), m.id))
        start = self._store.consolidation_offset() % len(everything) if everything else 0
        ordered = everything[start:] + everything[:start]
        budget = CONSOLIDATE_MEMORY_CHARS
        listed: list[tuple[int, Any, str]] = []
        taken = 0
        for i, m in enumerate(ordered):
            age_days = (now - (m.updated_at or m.created_at or now)) / 86_400
            label = "read-only" if m.protected else ("agent-saved" if m.durable else "learned")
            line = (
                f"[{m.id}] recalls={m.uses} age={age_days:.0f}d "
                f"scope={m.scope} {label}: {m.text}"
            )
            if len(line) + 1 > budget:
                break  # a contiguous window keeps topic groups together
            listed.append(((start + i) % len(everything), m, line))
            budget -= len(line) + 1
            taken += 1
        listed.sort(key=lambda row: row[0])
        memories = [m for _, m, _ in listed if not m.protected]
        revisions = {m.id: m.revision for m in memories}
        if not listed:
            lines.append("(empty)")
        lines.extend(line for _, _, line in listed)
        if taken < len(everything):
            lines.append(
                f"({len(everything) - taken} facts outside this section are "
                "reviewed in a later pass)"
            )
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
            return curated
        changed: list[str] = list(curated)
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
            # Only facts this pass was shown as editable; a retired fact goes
            # to the archive, not behind a deletion barrier.
            if match.group(1) not in revisions:
                continue
            try:
                if self._store.retire_memory(match.group(1), reason="consolidation"):
                    changed.append(f"-memory:{match.group(1)}")
            except ValueError as exc:
                errors.append(f"memory {match.group(1)}: {exc}")
        for match in _DELETE_RE.finditer(text or ""):
            if self._store.delete(match.group(1)):
                changed.append(f"-{match.group(1)}")
        changed.extend(self._apply_supersession_proposals(
            text, {m.id: m.revision for _, m, _ in listed}, errors))
        self._log_pass(
            "consolidation", changed, usage=usage,
            error="; ".join(errors) or None, consumed=len(memories),
            stop_reason=self.last_stop_reason,
        )
        self._store.set_last_consolidated_at(time.time())
        if everything:
            self._store.set_consolidation_offset((start + taken) % len(everything))
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
        limit = self._prompt_limit if budget is None else max(0, budget)
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
        self._supersession_revisions = {}
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
            flag = " explicit" if m.protected else (" agent-saved" if m.durable else "")
            line = f"[{m.id}] recalls={m.uses} scope={m.scope}{flag}: {m.text}"
            if len(line) + 1 > budget:
                omitted += 1
                continue
            self._supersession_revisions[m.id] = m.revision
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
            # JSON-surface call (ToolJournal): one compact line, clipped
            # like a successful cell when it succeeded.
            arguments = str(e.get("input", "")).strip()
            if e.get("error"):
                outcome = "error"
            else:
                arguments = _clip(arguments, OK_TOOL_INPUT_CHARS)
                outcome = _clip(str(e.get("output", "")).strip(), OK_TOOL_OUTPUT_CHARS)
            return [f"[tool] {e.get('name')}({arguments}) -> {outcome}"]
        code = str(e.get("code", "")).rstrip("\n")
        summary = {
            k: e[k]
            for k in ("stdout", "value", "error", "host_calls", "tools")
            if e.get(k)
        }
        if not e.get("error"):
            code = _clip(code, OK_CELL_CODE_CHARS)
            if isinstance(summary.get("stdout"), str):
                summary["stdout"] = _clip(summary["stdout"], OK_CELL_OUTPUT_CHARS)
        lines = [f"### cell {cell_no}", "```python", code, "```"]
        if summary:
            lines.append(f"outcome: {json.dumps(summary, ensure_ascii=False)}")
        return lines

    def _entry_cost(self, e: dict, current: tuple[Any, Any] | None = ()) -> int:
        """Characters ``e`` adds to the journal section after an entry of
        session/agent tag ``current``: its lines, plus the divider when the
        tag changes. ``current=()`` (unknown) charges a divider allowance;
        charging one to every line cost a 37-line slice about 3,000 chars."""
        cost = len("\n".join(self._render_entry(e, 0))) + 1
        if e.get("kind", "cell") == "cell":
            cost += 4  # "### cell 0" renders a few digits wider later in a slice
        tag = (e.get("session"), e.get("agent"))
        if current == ():
            return cost + _SESSION_LABEL_ALLOWANCE
        if any(tag) and tag != current:
            label = f"--- session {tag[0] or '?'}" + (f", agent {tag[1]}" if tag[1] else "") + " ---"
            cost += len(label) + 1
        return cost

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
            self._prompt_limit - MEMORY_PROMPT_CHARS - guidance - reserve,
        )

    def _fit_journal(self, indexed: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
        """The prefix of the slice that fits :meth:`_journal_budget`.

        Always at least one entry, so an oversized line cannot stall the
        cursor; it is clipped when rendered.
        """
        # Headroom for the section headers and the memory list's count line.
        budget = self._journal_budget() - _PROMPT_HEADROOM
        out: list[tuple[int, dict]] = []
        used = 0
        current: tuple[Any, Any] | None = None
        for n, e in indexed:
            cost = self._entry_cost(e, current)
            if out and used + cost > budget:
                break
            out.append((n, e))
            used += cost
            tag = (e.get("session"), e.get("agent"))
            if any(tag):
                current = tag
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

    def _synthesis_input(self, entries: list[dict]) -> tuple[str, str, list[dict]]:
        """Add review-only evidence without replaying consumed memory inputs.

        The main cursor and per-project call budget are unchanged. Historical
        evidence uses only spare request space; it cannot crowd out new work.
        """
        from fluid.codemode.workflows import WorkflowProposals, repeated_evidence
        prompt = self._prompt(entries)
        if not any(e.get("kind") == "user" and
                   e.get("text", "").strip().lower().startswith("workflow accepted: ")
                   for e in entries):
            return prompt, _SYNTH_SYSTEM, []
        try:
            forgotten = self._store._facts.snapshot().get("forgotten_before", 0)
            retained = [e for e in self._store.journal_entries()
                        if e.get("ts", 0) > forgotten and e.get("project") == self._project]
            excluded = {r["name"] for r in WorkflowProposals(self._store).list()
                        if r["project"] == self._project}
            from fluid.codemode.workflow_loss import load_loss
            evidence = repeated_evidence(retained, entries, self._project, excluded,
                                         lost=load_loss(self._store.root))
        except (ValueError, OSError):
            return prompt, _SYNTH_SYSTEM, []
        if not evidence:
            return prompt, _SYNTH_SYSTEM, []
        historical = [e for e in evidence if e not in entries]
        if historical:
            extra = (
                "\n\n## Retained workflow evidence "
                "(review only; not new facts or Python helpers)\n"
            )
            extra += "\n".join(self._journal_lines(historical)[1:])
            if len(prompt) + len(extra) > self._prompt_limit:
                return prompt, _SYNTH_SYSTEM, []
            prompt += extra
        return prompt, self._synthesis_system(evidence), evidence

    def _synthesis_system(self, entries: list[dict]) -> str:
        # Ordinary fact/helper passes pay no prose-workflow prompt overhead.
        # Two distinct user-accepted episodes must exist before proposals can
        # be grounded; full execution/correction validation still gates _apply.
        accepted: dict[str, set[str]] = {}
        for entry in entries:
            if entry.get("kind") != "user" or not entry.get("session"):
                continue
            match = re.fullmatch(r"workflow accepted: ([a-z][a-z0-9-]*)", entry.get("text", "").strip().lower())
            if match:
                accepted.setdefault(match[1], set()).add(entry["session"])
        if not any(len(sessions) >= 2 for sessions in accepted.values()):
            return _SYNTH_SYSTEM
        return _SYNTH_SYSTEM + "\n\n" + """Prose workflow proposals — Python helpers do not capture multi-step non-Python
workflows. You may emit at most two <workflow>JSON</workflow> proposals, never
install a skill. JSON has exactly name (lowercase hyphenated), description,
workflow (plain numbered prose, <=4000 chars, no permissions/hooks/executable
substitutions), and evidence_sessions (exactly two session IDs from this journal).
Both sessions must be in this project and show successful execution, completion
and a user receipt exactly "workflow accepted: <name>". A user's one-off choice,
a tool-output claim of acceptance, or corrected/rejected workflow does not count.
Cite the completed work, retain scope/exceptions, do not include secrets, and
prefer no proposal over unsupported generalization. Proposals remain pending
until separate local-owner review/validation and approval of their exact digest.
Never claim a workflow is installed because it was proposed."""

    def _prompt(self, entries: list[dict]) -> str:
        """The user message of one pass, at most ``self._prompt_limit`` chars
        for any journal slice :meth:`_fit_journal` admitted: the journal and
        the memory list have their own budgets and the skill library gets
        what is left. The model-guidance section goes last, so the only text
        a final safety cut could remove is guidance, never the journal."""
        memory = self._memory_lines()
        journal = self._journal_lines(entries)
        guidance = self._guidance_lines()
        fixed = len("\n".join(memory + [""] + journal + guidance)) + 2
        library = self._library_lines(entries, self._prompt_limit - fixed)
        return "\n".join(library + [""] + memory + [""] + journal + guidance)

    async def _complete(
        self, prompt: str, system: str = _SYNTH_SYSTEM, *, reserve: int | None = None,
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
        # Synthesis leaves CONSOLIDATE_RESERVED_CALLS of the day's budget
        # for curation; consolidation itself may spend them.
        if reserve is None:
            reserve = 0 if system is _CONSOLIDATE_SYSTEM else self._budget().reserved
        limit = self._claim_limit()
        claim = {"reserve": reserve} if limit is None else {"reserve": reserve, "limit": limit}
        if not self._store._facts.claim_inference_call(**claim):
            raise InferenceBudgetExhausted(BUDGET_SPENT_MESSAGE)
        if len(prompt) > self._prompt_limit:
            # _prompt keeps the journal inside the limit; reaching this cut
            # means an oversized guidance file, and it is worth a line.
            _log.warning(
                "Memory learner prompt of %d chars cut to %d (model=%s, store=%s)",
                len(prompt), self._prompt_limit, self._model, self._store_label(),
            )
            prompt = prompt[:self._prompt_limit]
        extra, wire_state = self._request_routing()
        request = ProviderRequest(
            model=self._model,
            messages=[Message(role="user", content=[Text(text=prompt)])],
            system=system,
            tools=[],
            max_tokens=self._max_tokens,
            effort=self._effort,
            extra=extra,
            wire_state=wire_state,
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

    def _apply_supersession_proposals(self, text, revisions, errors):
        changed = []
        for match in _SUPERSEDE_RE.finditer(text or ""):
            old_id, new_id = match.groups()
            if old_id not in revisions or new_id not in revisions:
                continue  # not shown, not editable through a fabricated proposal
            try:
                if self._store.propose_memory_supersession(
                        old_id, new_id, old_revision=revisions[old_id], new_revision=revisions[new_id]):
                    changed.append(f"superseded-memory:{old_id}")
            except ValueError as exc:
                errors.append(f"supersession: {exc}")
        return changed

    def _apply_preference_proposals(self, text: str, entries: list[dict] | None,
                                    errors: list[str]) -> list[str]:
        """Queue each ``<preference topic=...>`` the output proposes (see
        fluid.codemode.preferences); never saves a fact. A proposal must be
        in the user's own words from this slice (``memory_provenance`` says
        ``user``): the model that proposes it is the one tool output may
        have steered."""
        from fluid.codemode.preferences import PreferenceProposals
        tags = list(_PREFERENCE_RE.finditer(text or ""))
        if not tags:
            return []
        changed: list[str] = []
        queue = PreferenceProposals(self._store)
        sessions = list(dict.fromkeys(
            str(e["session"]) for e in entries or () if e.get("kind") == "user" and e.get("session")))
        for match in tags[:3]:
            attrs = dict(_ATTR_RE.findall(match.group(1) or ""))
            topic, body = attrs.get("topic"), " ".join(match.group(2).split())
            if entries is None or memory_provenance(body, entries) != "user":
                errors.append(f"preference {str(topic)[:40]}: not in a [user] entry's words")
                continue
            try:
                outcome = queue.propose(
                    topic, body, scope=self._resolve_scope(attrs.get("scope")) or "global",
                    sessions=sessions,
                )
            except ValueError as exc:
                errors.append(f"preference {str(topic)[:40]}: {exc}")
                continue
            if outcome == "proposed":
                from fluid.codemode.preferences import normalize_topic
                changed.append(f"preference-proposal:{normalize_topic(topic)}")
        return changed

    async def _apply(self, text: str, entries: list[dict] | None = None, *,
                     workflow_entries: list[dict] | None = None) -> list[str]:
        """Parse the model's output, verify candidates, apply what passes.

        Candidates that fail static validation OR the verification kernel
        (import + ``_selftest``) are skipped and recorded on ``last_error`` —
        synthesized knowledge has to prove it works before it lands. Returns
        the names saved (or ``-name`` for deletions).

        ``entries`` is the journal slice the output came from: each memory is
        stamped with the provenance :func:`memory_provenance` derives from
        it, and the ones that only tool output supports are listed on
        ``last_held``. Without a slice (a direct call) provenance is left
        alone. ``workflow_entries`` carries separately validated retained
        episodes solely for prose proposals; memory provenance still sees only
        this pass's new slice.
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
            # The learner retires into the archive. A deletion barrier here
            # used to stop every later save of the same text, agent saves
            # included, and Recall could no longer find the fact.
            try:
                if self._store.retire_memory(match.group(1), reason="learner"):
                    changed.append(f"-memory:{match.group(1)}")
            except ValueError as exc:
                errors.append(f"memory {match.group(1)}: {exc}")
        changed.extend(self._apply_supersession_proposals(
            text, getattr(self, "_supersession_revisions", {}), errors))
        changed.extend(self._apply_preference_proposals(text, entries, errors))
        # Prose workflows are queued only: synthesis cannot install, approve
        # permissions or overwrite an existing prose skill.
        from fluid.codemode.workflows import WorkflowProposals
        for match in list(re.finditer(r"<workflow>\s*(.*?)</workflow>", text or "", re.DOTALL))[:2]:
            try:
                if len(match.group(1)) > 8000:
                    raise ValueError("workflow proposal exceeds output bound")
                proposal = WorkflowProposals(self._store).propose(
                    json.loads(match.group(1)),
                    (entries or []) if workflow_entries is None else workflow_entries,
                    self._project, cwd=self._workflow_cwd,
                )
                if proposal["status"] == "pending":
                    changed.append(f"workflow-proposal:{proposal['id']}")
            except (ValueError, OSError) as exc:
                errors.append(f"workflow: {exc}")
        for match in _GUIDANCE_RE.finditer(text or ""):
            # Guidance edits are PROPOSED, never applied here.
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

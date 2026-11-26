"""Session reflection: how the memory learner learns in Memory v3 phase 2.

The continuous learner (:mod:`fluid.codemode.continual`) read the journal
every few turns and wrote facts, rules and skills as it went. Reflection
replaces it: a session is read once, when it settles or goes quiet, in at
most one model call, and only when the session gave a reason to.

- **When.** :meth:`Synthesizer.drain` runs when the chat's engine closes:
  the chat pool evicts an idle engine (minutes after the last turn), the
  chat is settled, archived or deleted, or the worker shuts down. A chat
  reopened later gets a new engine, and its next close reflects only the
  turns after the ones already reflected (:class:`ReflectionState`).
- **Whether.** :func:`gate` is deterministic and makes no call: the new
  part of the session needs a correction or frustration from the user, an
  explicit "remember this", or friction that repeated. A session without one
  is logged as skipped with its reason.
- **What it reads.** The session's own user turns, the assistant's replies,
  its friction records and the facts and rules in scope; never the tool
  stream. Bounded by :data:`REFLECTION_INPUT_CHARS` (about 7k tokens).
- **Where it goes.** A rule or a preference quoting the owner's words is
  saved at once as a learned behaviour rule (agent tier, revertible on
  Settings > Learned behaviour). Everything else is a *proposal* in
  :class:`ProposalInbox`: it becomes an agent-saved fact only when the owner
  accepts it, and otherwise expires after 14 days. A dismissed proposal never
  comes back. An open proposal is not a fact: it is never in
  ``memories.json``, so it never takes a slot, never reaches the index,
  just-in-time delivery or Recall, and is never counted as a dropped fact.
  The same proposal from several chats (equal after case, spacing and
  punctuation) is one row that lists them all; that count is information
  for the owner, never a reason to save.

The worker-wide rollback switch ``FLUID_MEMORY_LEARNER=continuous`` brings
back the continuous learner (skills, consolidation, when-line backfill and
workflow proposals included). Phase 3 deletes it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import weakref
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Iterator

LEARNER_SWITCH = "FLUID_MEMORY_LEARNER"
CONTINUOUS = "continuous"
REFLECTION = "reflection"

#: Journal kinds reflection reads. Tool calls and code cells are left out.
CONVERSATION_KINDS = ("user", "assistant", "remember", "friction")
#: The whole reflection prompt, memory and rules included (about 7k tokens).
REFLECTION_INPUT_CHARS = 28_000
USER_TURN_CHARS = 2_000
ASSISTANT_TURN_CHARS = 800
FRICTION_CHARS = 300
MAX_PROPOSALS_PER_PASS = 5
#: A (signal, tool) friction pair seen this often in one session repeated.
REPEATED_FRICTION = 2

PROPOSAL_FILE = "proposals.json"
REMOTE_PROPOSAL_FILE = "proposals.remote.json"
STATE_FILE = ".reflection-state.json"
PROPOSAL_LOCK = ".proposals"
STATE_LOCK = ".reflection-state"
PROPOSAL_TTL_SECONDS = 14 * 86400
MAX_PROPOSALS = 100
MAX_PROPOSAL_CHARS = 500
MAX_STATE_SESSIONS = 500
#: How long a writer waits for another process holding the inbox or the
#: reflection state. Writes take milliseconds; this only bites when stuck.
LOCK_WAIT_SECONDS = 5.0
#: How long a close waits for an earlier close of the same chat to finish
#: reflecting on this worker before it is skipped as busy (and retried at the
#: chat's next close). Other chats never wait for it.
REFLECTION_WAIT_SECONDS = 60.0

OWNER_RULE = "owner_rule"
OWNER_PREFERENCE = "owner_preference"
OWNER_FACT = "owner_fact"
PROJECT = "project"
PROPOSAL_KINDS = (OWNER_PREFERENCE, OWNER_FACT, PROJECT)
KIND_LABELS = {
    OWNER_RULE: "Owner rule",
    OWNER_PREFERENCE: "Owner preference",
    OWNER_FACT: "Owner fact",
    PROJECT: "Project knowledge",
}
OPEN, ACCEPTED, DISMISSED = "open", "accepted", "dismissed"
DECISIONS = (ACCEPTED, DISMISSED)
_DEFAULT_WHEN = {
    OWNER_PREFERENCE: "When working with the owner (learned preference)",
    OWNER_FACT: "When working with the owner",
    PROJECT: "When working in this project",
}


class InboxBusy(OSError):
    """Another writer held the inbox or the reflection state for the whole
    wait. Nothing was written."""


class ProposalDecided(LookupError):
    """The proposal was accepted, dismissed or expired before this decision."""


def learner_mode() -> str:
    """``continuous`` when the worker's ``FLUID_MEMORY_LEARNER`` says so (a
    temporary rollback switch, read on every call), else ``reflection``."""
    raw = os.environ.get(LEARNER_SWITCH, "").strip().lower()
    return CONTINUOUS if raw == CONTINUOUS else REFLECTION


def reflection_mode() -> bool:
    return learner_mode() == REFLECTION


# ── the gate ───────────────────────────────────────────────────────────────
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_REMEMBER_RE = re.compile(
    r"\b(?:remember\s+(?:this|that|it|to)|don'?t\s+forget|do\s+not\s+forget"
    r"|keep\s+(?:this|that|it\s+)?in\s+mind|make\s+a\s+note|note\s+(?:this|that)\s+(?:down|for)"
    r"|for\s+future\s+reference)\b",
    re.IGNORECASE,
)
_CORRECTION_RE = re.compile(
    r"(?:^\s*(?:no|nope|wrong|not\s+that)\b[,.!\s]"
    r"|\b(?:don'?t|do\s+not|stop|instead|rather\s+than|i\s+prefer|i(?:\s+would|'d)\s+(?:prefer|rather)"
    r"|from\s+now\s+on|going\s+forward|next\s+time|in\s+(?:the\s+)?future|you\s+should\s+have"
    r"|i\s+meant|that'?s\s+not\s+(?:right|it|what))\b)",
    re.IGNORECASE,
)


def _user_text(entry: dict) -> str:
    return _CODE_RE.sub(" ", str(entry.get("text") or ""))[:4_000]


def gate(entries: Iterable[dict], *, continued: bool = False) -> list[str]:
    """Why a session is worth one reflection call, or ``[]`` when it is not.

    ``frustration``: a user turn the journal stamped as frustrated
    (behavior.frustration_signal). ``correction``: a user turn that corrects
    or redirects the agent; only a turn after an assistant reply counts, so
    a first request that says "don't touch the tests" is not a correction.
    ``remember``: the user asked for something to be remembered.
    ``friction``: the same harness friction (signal and tool) twice or more,
    or a failure streak. No model is called to decide. ``continued``: the
    entries continue a session reflected before (its last reply came first)."""
    signals: list[str] = []
    friction: Counter = Counter()
    replied = continued
    for entry in entries:
        kind = entry.get("kind")
        if kind == "assistant":
            replied = True
        elif kind == "user":
            text = _user_text(entry)
            if entry.get("frustration") and "frustration" not in signals:
                signals.append("frustration")
            if replied and _CORRECTION_RE.search(text) and "correction" not in signals:
                signals.append("correction")
            if _REMEMBER_RE.search(text) and "remember" not in signals:
                signals.append("remember")
        elif kind == "friction":
            signal = str(entry.get("signal") or "")
            friction[(signal, str(entry.get("name") or ""))] += 1
            if signal == "error_streak":
                friction[(signal, "*streak*")] += REPEATED_FRICTION
    if any(count >= REPEATED_FRICTION for count in friction.values()):
        signals.append("friction")
    return signals


# ── small file helpers ──────────────────────────────────────────────────────
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value: Any) -> None:
    from fluid.codemode.skills import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True))


@contextmanager
def _locked(root: Path, name: str) -> Iterator[None]:
    """Hold ``root/name``'s advisory lock inside the block, or raise
    :class:`InboxBusy` without entering it: a writer that could not take the
    lock never writes."""
    from fluid.codemode.skills import journal_lock

    Path(root).mkdir(parents=True, exist_ok=True)
    with journal_lock(Path(root) / name, wait=LOCK_WAIT_SECONDS) as held:
        if not held:
            raise InboxBusy(f"{name.lstrip('.')} is held by another writer; nothing was written")
        yield


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ── which sessions were reflected ───────────────────────────────────────────
class ReflectionState:
    """How far each session was reflected (the newest journal ``ts`` read),
    so a session that settles again is reflected only on its new part.
    Bounded to the newest :data:`MAX_STATE_SESSIONS` sessions."""

    def __init__(self, root: Path):
        self.path = Path(root) / STATE_FILE

    def _sessions(self) -> dict[str, dict]:
        doc = _read_json(self.path)
        sessions = doc.get("sessions") if isinstance(doc, dict) else None
        return sessions if isinstance(sessions, dict) else {}

    def through(self, session: str) -> float:
        row = self._sessions().get(session)
        return _float(row.get("through")) if isinstance(row, dict) else 0.0

    def all_through(self) -> dict[str, float]:
        return {sid: _float(row.get("through")) for sid, row in self._sessions().items()
                if isinstance(row, dict)}

    def mark(self, session: str, through: float, *, now: float | None = None) -> None:
        """Record ``session`` as reflected through ``through``. InboxBusy
        (nothing written) when another process held the state."""
        with _locked(self.path.parent, STATE_LOCK):
            sessions = self._sessions()
            sessions[session] = {"through": float(through), "at": float(now or time.time())}
            if len(sessions) > MAX_STATE_SESSIONS:
                newest = sorted(sessions.items(), key=lambda kv: -_float(kv[1].get("at")))
                sessions = dict(newest[:MAX_STATE_SESSIONS])
            _write_json(self.path, {"schema": 1, "sessions": sessions})


# ── the proposal inbox ──────────────────────────────────────────────────────
def normalize(text: str) -> str:
    """``text`` compared as a proposal: case, spacing and punctuation aside,
    every word kept in order ("do not" stays apart from "do")."""
    return " ".join(re.sub(r"[\W_]+", " ", str(text or "").casefold()).split())


def proposal_id(kind: str, scope: str, text: str) -> str:
    basis = f"{kind}|{scope}|{normalize(text)}"
    return "p" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:15]


def _valid_item(item: Any) -> bool:
    return (isinstance(item, dict) and isinstance(item.get("id"), str)
            and item.get("kind") in PROPOSAL_KINDS and isinstance(item.get("text"), str)
            and isinstance(item.get("sessions"), list))


def _clean_decisions(raw: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for pid, row in raw.items():
            if isinstance(pid, str) and isinstance(row, dict) and row.get("status") in DECISIONS:
                kept = {"status": row["status"], "at": _float(row.get("at"))}
                if isinstance(row.get("fact_id"), str):
                    kept["fact_id"] = row["fact_id"]
                out[pid] = kept
    return out


def _parse(doc: Any) -> tuple[list[dict], dict[str, dict]]:
    """(proposals, decisions) from one worker's inbox document. A list is
    proposals alone; a decided row of the first format is a decision."""
    if isinstance(doc, list):
        items, decisions = doc, {}
    elif isinstance(doc, dict):
        items, decisions = doc.get("proposals"), _clean_decisions(doc.get("decisions"))
    else:
        return [], {}
    rows: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not _valid_item(item):
            continue
        if item.get("status") in DECISIONS:
            decisions.setdefault(item["id"], {"status": item["status"],
                                              "at": _float(item.get("decided_at"))})
        else:
            rows.append(item)
    return rows, decisions


def _live(rows: Iterable[dict], now: float) -> list[dict]:
    """Rows that have not expired. An expired row is gone before any lookup
    or merge, so the same proposal later starts a fresh 14 days."""
    return [r for r in rows if _float(r.get("expires_at")) > now]


class ProposalInbox:
    """Proposals reflection was not sure enough to save, kept in the owner's
    store directory, outside the fact store.

    ``proposals.json`` holds this worker's open proposals and every decision
    the owner made on any worker. Decisions are kept for good (an id, a
    status and a time each): a dismissed proposal never comes back, and an
    accepted one is not offered again. ``proposals.remote.json`` holds what
    other workers published (app.network.memory_records); the inbox shows the
    merged view. Phase 3 moves this to SQLite."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / PROPOSAL_FILE
        self.remote_path = self.root / REMOTE_PROPOSAL_FILE

    # storage
    def _local(self) -> tuple[list[dict], dict[str, dict]]:
        return _parse(_read_json(self.path))

    def _remote(self) -> dict[str, tuple[list[dict], dict[str, dict]]]:
        doc = _read_json(self.remote_path)
        devices = doc.get("devices") if isinstance(doc, dict) else None
        if not isinstance(devices, dict):
            return {}
        return {str(device): _parse(value) for device, value in devices.items()}

    def _save(self, rows: list[dict], decisions: dict[str, dict], now: float) -> None:
        kept = [r for r in _live(rows, now) if r["id"] not in decisions]
        kept.sort(key=lambda r: -_float(r.get("updated_at") or r.get("created_at")))
        _write_json(self.path, {"schema": 2, "proposals": kept[:MAX_PROPOSALS],
                                "decisions": decisions})

    def _announce(self) -> None:
        """Tell the host this worker's proposals changed (it republishes
        them; see app.network.memory_records). After the lock is released."""
        from fluid.codemode import transactions

        transactions.committed_change(self.path)

    def export(self, now: float | None = None) -> dict:
        """This worker's open proposals and its decisions, for replication."""
        now = now or time.time()
        rows, decisions = self._local()
        return {"proposals": [r for r in _live(rows, now) if r["id"] not in decisions],
                "decisions": decisions}

    def apply_remote(self, device: str, items: list[dict] | None,
                     decisions: dict | None = None, *, now: float | None = None) -> None:
        """Keep another worker's published proposals and decisions (None:
        it has none left). Its decisions are also kept here for good, so a
        dismissal outlives that worker's record. Under the inbox lock, like
        every local decision."""
        now = now or time.time()
        rows, theirs = _parse({"proposals": items or [], "decisions": decisions or {}})
        with _locked(self.root, PROPOSAL_LOCK):
            doc = _read_json(self.remote_path)
            devices = doc.get("devices") if isinstance(doc, dict) else None
            devices = devices if isinstance(devices, dict) else {}
            if rows or theirs:
                devices[str(device)] = {"proposals": rows[:MAX_PROPOSALS], "decisions": theirs}
            else:
                devices.pop(str(device), None)
            _write_json(self.remote_path, {"schema": 2, "devices": devices})
            local_rows, mine = self._local()
            adopted = {pid: row for pid, row in theirs.items() if pid not in mine}
            if adopted:
                self._save(local_rows, {**mine, **adopted}, now)

    # the merged view
    def _decisions(self) -> dict[str, dict]:
        """Every decision on any worker; the earliest one wins."""
        out: dict[str, dict] = {}
        sources = [self._local()[1]] + [d for _, d in self._remote().values()]
        for decisions in sources:
            for pid, row in decisions.items():
                if pid not in out or row["at"] < out[pid]["at"]:
                    out[pid] = row
        return out

    def _merged(self, now: float) -> tuple[dict[str, dict], dict[str, dict]]:
        """(open proposals by id, decisions) over this worker and the others.
        An open proposal joins its sessions across workers and expires at the
        latest; expired and decided ones are left out."""
        decisions = self._decisions()
        merged: dict[str, dict] = {}
        sources = [("local", self._local()[0])] + [(d, rows) for d, (rows, _) in self._remote().items()]
        for origin, rows in sources:
            for item in _live(rows, now):
                if item["id"] in decisions:
                    continue
                row = merged.get(item["id"])
                if row is None:
                    row = merged[item["id"]] = {**item, "sessions": [], "origins": [],
                                                "status": OPEN, "expires_at": 0.0}
                for sid in item.get("sessions") or []:
                    if isinstance(sid, str) and sid not in row["sessions"]:
                        row["sessions"].append(sid)
                row["origins"].append(origin)
                row["expires_at"] = max(_float(row["expires_at"]), _float(item.get("expires_at")))
                row["created_at"] = min(_float(row.get("created_at")) or now,
                                        _float(item.get("created_at")) or now)
                if not row.get("quote") and item.get("quote"):
                    row["quote"] = item["quote"]
        return merged, decisions

    def _ever_listed(self, pid: str) -> bool:
        """True when ``pid`` is in any worker's file, expired rows included."""
        sources = [self._local()[0]] + [rows for rows, _ in self._remote().values()]
        return any(r["id"] == pid for rows in sources for r in rows)

    def open(self, now: float | None = None) -> list[dict]:
        """Open, unexpired proposals, newest first."""
        rows = list(self._merged(now or time.time())[0].values())
        rows.sort(key=lambda r: -_float(r.get("updated_at") or r.get("created_at")))
        return rows

    def get(self, pid: str, now: float | None = None) -> dict | None:
        return self._merged(now or time.time())[0].get(pid)

    def decision(self, pid: str) -> dict | None:
        return self._decisions().get(pid)

    # writes
    def propose(self, *, kind: str, text: str, scope: str, session: str,
                quote: str | None = None, when: str | None = None,
                now: float | None = None) -> dict:
        """Add a proposal, or this session to the same open one (equal after
        :func:`normalize`). Returns the merged row; one the owner already
        decided comes back with that decision as its status and is not
        added."""
        if kind not in PROPOSAL_KINDS:
            raise ValueError(f"unknown proposal kind {kind!r}")
        now = now or time.time()
        pid = proposal_id(kind, scope, text)
        with _locked(self.root, PROPOSAL_LOCK):
            decided = self._decisions().get(pid)
            if decided is not None:
                return {"id": pid, "kind": kind, "scope": scope, "text": text, **decided}
            rows, decisions = self._local()
            rows = _live(rows, now)
            mine = next((r for r in rows if r["id"] == pid), None)
            if mine is None:
                mine = {"id": pid, "kind": kind, "scope": scope, "text": text, "when": when,
                        "quote": quote, "sessions": [], "created_at": now,
                        "expires_at": now + PROPOSAL_TTL_SECONDS, "status": OPEN}
                rows.append(mine)
            if session in mine["sessions"] and (mine.get("quote") or not quote):
                # This chat queued it already (a retried reflection): nothing
                # to write, and the caller is told it is not new.
                return {**mine, "repeat": True}
            if session not in mine["sessions"]:
                mine["sessions"].append(session)
            if not mine.get("quote") and quote:
                mine["quote"] = quote
            mine["updated_at"] = now
            self._save(rows, decisions, now)
        self._announce()
        return self._merged(now)[0].get(pid) or mine

    def dismiss(self, pid: str, *, now: float | None = None) -> dict:
        """Dismiss an open proposal for good. KeyError when it is unknown,
        :class:`ProposalDecided` when it was decided or expired first."""
        row, _ = self._decide(pid, DISMISSED, None, now)
        return row

    def accept(self, pid: str, save: Callable[[dict], Any], *,
               now: float | None = None) -> tuple[dict, Any]:
        """Accept an open proposal: under the inbox lock it must still be
        open, ``save(row)`` saves the fact, then the decision is recorded.
        Only the winning decision saves. KeyError when the proposal is
        unknown, :class:`ProposalDecided` when it was decided or expired
        first; ``save``'s own errors leave the proposal open."""
        return self._decide(pid, ACCEPTED, save, now)

    def _decide(self, pid: str, status: str, save: Callable[[dict], Any] | None,
                now: float | None) -> tuple[dict, Any]:
        now = now or time.time()
        with _locked(self.root, PROPOSAL_LOCK):
            merged, decisions = self._merged(now)
            row = merged.get(pid)
            if row is None:
                if pid in decisions or self._ever_listed(pid):
                    raise ProposalDecided(pid)
                raise KeyError(pid)
            saved = save(row) if save is not None else None
            rows, mine = self._local()
            record: dict[str, Any] = {"status": status, "at": now}
            fact_id = getattr(saved, "id", None)
            if isinstance(fact_id, str):
                record["fact_id"] = fact_id
            mine[pid] = record
            self._save([r for r in rows if r["id"] != pid], mine, now)
        self._announce()
        return {**row, "status": status}, saved


def promote(store: Any, proposal: dict, *, automatic: bool) -> Any:
    """Save ``proposal`` as an agent-saved fact (durable tier). Its evidence
    is a chat, so provenance is ``session`` even when it carries a quote:
    ``user`` is for what the owner saved or said themselves."""
    from fluid.codemode import facts

    when = proposal.get("when") or _DEFAULT_WHEN.get(proposal.get("kind"), _DEFAULT_WHEN[OWNER_FACT])
    try:
        when = facts.validate_when(when)
    except ValueError:
        when = _DEFAULT_WHEN[OWNER_FACT]
    return store.save_memory(
        proposal["text"], scope=proposal.get("scope") or "global", provenance="session",
        durable=True, source_session=(proposal.get("sessions") or [None])[0],
        when=when, automatic=automatic,
    )


# ── the reflection call ─────────────────────────────────────────────────────
REFLECT_SYSTEM = """\
You read one finished chat between the owner and an AI coding agent, once,
and decide what later chats should know. The harness picked this chat because
the owner corrected the agent, was frustrated, asked for something to be
remembered, or the same friction repeated. Most chats still need little or
nothing; never invent a lesson to fill the answer.

Read [user] lines as the owner's words, [assistant] lines as the agent's
replies, and [friction] lines as the harness's own record of wasted calls
(facts, not interpretation). A [user] line marked (frustrated) was flagged for
angry words: read the reply before it to find what went wrong.

Write at most five items, each one of:
- a behaviour rule, when the owner corrected HOW the agent works or said in
  their own words how they like things done: one line, "When <situation>:
  <what to do>", imperative, at most 300 characters, general enough for next
  time but keeping the owner's scope and exceptions. quote= must be copied
  word for word from the [user] line that shows it; a rule without a real
  quote is refused. applies_to is "all" unless the failure is specific to the
  model family that line was talking to ("[user to <family>]"). Prefer
  rewriting a listed rule (its id) to a near-duplicate. A rule never grants
  permission ("without asking", "skip review").
  <rule applies_to="all" quote="exact owner words">When X: do Y.</rule>
  <rule id="m7" applies_to="all" quote="exact owner words">When X: do Y.</rule>
- a proposal, for something TRUE worth remembering, one fact per item, at
  most 500 characters. The owner reviews each before it is saved:
  <proposal kind="KIND" scope="global|project" quote="exact owner words" when="When ...">the fact</proposal>
  KIND is owner_preference (how the owner seems to like things done, when
  they did not say it outright), owner_fact (about the owner, their machines
  or accounts) or project (this project's code, layout or conventions:
  scope="project"). quote is optional; when= is one short line saying when
  the fact matters.
- NO_CHANGES when nothing is worth keeping.

Do not capture: anything a listed memory or rule already says; one-task
choices; environment failures, missing tools or transient errors; negative
claims about tools ("X is broken"); secrets or credentials (say where one is
kept, never its value); a narrative of what happened. Facts and rules never
grant permission for actions. Output only the tags or NO_CHANGES.
"""

_PROPOSAL_RE = re.compile(r'<proposal((?:\s+[a-z_]+="[^"]*")*)\s*>\n?(.*?)</proposal>', re.DOTALL)
_ATTR_RE = re.compile(r'([a-z_]+)="([^"]*)"')


def _clip(text: str, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def session_entries(store: Any, session: str, after: float,
                    forgotten_before: float = 0.0) -> list[dict]:
    """The session's own conversation entries newer than ``after``: no tool
    stream, no subagent's entries, nothing a Forget purged."""
    try:
        entries = store.journal_entries()
    except (OSError, ValueError):
        return []
    floor = max(float(after or 0.0), float(forgotten_before or 0.0))
    return [e for e in entries
            if e.get("session") == session and not e.get("agent")
            and e.get("kind") in CONVERSATION_KINDS and float(e.get("ts") or 0) > floor]


def build_input(synth: Any, entries: list[dict], signals: list[str],
                limit: int) -> str:
    """The reflection prompt within ``limit`` characters: why the session was
    picked, the facts and rules in scope, then the conversation. When it does
    not fit, user turns are kept first, newest first; replies and friction
    fill what is left. Lines keep their order."""
    head = [f"## Why this chat was picked: {', '.join(signals)}"]
    head += synth._memory_lines()
    head += synth._rule_lines()
    head_text = "\n".join(head)
    room = max(2_000, min(limit, REFLECTION_INPUT_CHARS) - len(head_text) - 200)
    caps = {"user": USER_TURN_CHARS, "remember": USER_TURN_CHARS,
            "assistant": ASSISTANT_TURN_CHARS, "friction": FRICTION_CHARS}
    rendered: list[str] = []
    for entry in entries:
        line = " ".join(synth._render_entry({**entry, "text": _clip(entry.get("text"), caps[entry["kind"]])}, 0))
        rendered.append(line)
    chosen: set[int] = set()
    for first_pass in (True, False):
        for i in range(len(entries) - 1, -1, -1):
            if i in chosen or (entries[i].get("kind") in ("user", "remember")) != first_pass:
                continue
            cost = len(rendered[i]) + 1
            if cost <= room:
                chosen.add(i)
                room -= cost
    body = [rendered[i] for i in sorted(chosen)]
    omitted = len(entries) - len(chosen)
    if omitted:
        body.insert(0, f"({omitted} earlier lines of this chat left out for length)")
    return head_text + "\n\n## This chat\n" + "\n".join(body)


def _preference_rule(body: str, when: str | None) -> str:
    """A quoted preference as a behaviour rule: "When <situation>: <it>"."""
    if re.match(r"(?i)when\b[^:]{1,200}:", body):
        return body
    situation = (when or "").strip().rstrip(".:")
    if not re.match(r"(?i)when\b", situation):
        situation = "When working with the owner"
    return f"{situation}: {body}"


async def apply_output(synth: Any, text: str, entries: list[dict], session: str,
                       inbox: ProposalInbox, errors: list[str]) -> tuple[list[str], dict[str, int]]:
    """Save the rules (a quoted preference is one too) through the learner's
    behaviour-rule checks, and queue every other proposal for the owner.
    Returns the change names and counts by outcome. Inbox writes run in a
    worker thread: waiting on the inbox lock never blocks the event loop."""
    from fluid.codemode import behavior, facts

    # A retry after a storage error (see _reflect_turn) gets the same output
    # again: what it already saved is a repeat, never reported as new.
    repeats: list[str] = []
    changed = list(synth._apply_rules(text, entries, errors, repeats))
    counts = {"rules": 0, "proposed": 0}
    for match in list(_PROPOSAL_RE.finditer(text or ""))[:MAX_PROPOSALS_PER_PASS]:
        attrs = dict(_ATTR_RE.findall(match.group(1) or ""))
        kind = attrs.get("kind", "")
        label = f"proposal {match.group(2)[:30]!r}"
        if kind not in PROPOSAL_KINDS:
            errors.append(f"{label}: kind must be one of {', '.join(PROPOSAL_KINDS)}")
            continue
        try:
            body = facts.validate_text(" ".join(match.group(2).split()))
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if len(body) > MAX_PROPOSAL_CHARS:
            errors.append(f"{label}: longer than {MAX_PROPOSAL_CHARS} characters")
            continue
        when = attrs.get("when") or None
        if when:
            try:
                when = facts.validate_when(when)
            except ValueError:
                when = None
        quote = attrs.get("quote") or None
        if quote and not behavior.quote_in_user_text(quote, entries):
            quote = None
        scope_attr = "project" if (attrs.get("scope") or
                                   ("project" if kind == PROJECT else "global")) == "project" else "global"
        if (kind == OWNER_PREFERENCE and quote and "<" not in body
                and (scope_attr == "global" or synth._project)):
            # The owner said it: a learned behaviour rule, with the same
            # quote, permission, injection and cap checks as any rule, and
            # the proposal's scope: a preference about one repository is a
            # rule there only. One those checks refuse waits for the owner.
            tag = (f'<rule applies_to="all" scope="{scope_attr}" quote="{quote}">'
                   f"{_preference_rule(body, when)}</rule>")
            refused: list[str] = []
            rules = synth._apply_rules(tag, entries, refused, repeats)
            if rules:
                changed.extend(rules)
                continue
            errors.extend(f"{label}: {reason}" for reason in refused)
        scope = synth._resolve_scope(scope_attr) or "global"
        try:
            row = await asyncio.to_thread(inbox.propose, kind=kind, text=body, scope=scope,
                                          session=session, quote=quote, when=when)
        except OSError as exc:
            # Storage, not the proposal (InboxBusy is one): the turns stay
            # unreflected so the chat's next close pays for them again.
            errors.append(f"{label}: not saved ({exc})")
            counts["unsaved"] = counts.get("unsaved", 0) + 1
            continue
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if row.get("status") != OPEN or row.get("repeat"):
            continue  # decided already (it stays decided), or queued from this chat before
        changed.append(f"proposal:{row['id']}")
        counts["proposed"] += 1
    changed = [name for name in changed if name not in repeats]
    counts["rules"] = sum(1 for name in changed if name.startswith("rule:"))
    return changed, counts


def advance_cursor(store: Any, state: ReflectionState) -> None:
    """Move the learner's journal cursor past lines no reader needs any
    more, so the journal can be reaped: the tool stream, subagent lines,
    unstamped lines and every session's conversation already reflected. It
    stops at the first line of a session not reflected yet."""
    try:
        through = state.all_through()
        lease, cursor, indexed, reached = store.begin_synth_read(None)
    except (OSError, ValueError):
        return
    try:
        done = reached
        for n, entry in indexed:
            session = entry.get("session")
            if (entry.get("kind") in CONVERSATION_KINDS and session and not entry.get("agent")
                    and float(entry.get("ts") or 0) > through.get(str(session), -1.0)):
                done = n - 1
                break
        if done > cursor:
            store.commit_synth_cursor(lease, done)
    finally:
        store.end_synth_read(lease)
    try:
        store.reap_journal()
    except Exception:  # noqa: BLE001 — a failed reap never costs a reflection
        pass


#: One reflection at a time per chat (store and session) on this worker, per
#: event loop: a second close of the same chat waits for the first and then
#: re-reads its watermark; another chat never waits. An asyncio lock: waiting
#: for it never blocks the loop, and it is never a file lock held across the
#: model call.
_TURNS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


_USERS: "weakref.WeakKeyDictionary[asyncio.Lock, int]" = weakref.WeakKeyDictionary()


class _TurnBusy(Exception):
    """Another close of the same chat held its reflection past the wait."""


@asynccontextmanager
async def _chat_turn(root: Path, session: str, timeout: float) -> AsyncIterator[None]:
    """Hold this chat's reflection lock; :class:`_TurnBusy` when another
    close of the same chat kept it past ``timeout``. A chat's entry is dropped once no
    close holds or waits for it, so the table stays as small as the closes
    in flight."""
    locks = _TURNS.setdefault(asyncio.get_running_loop(), {})
    key = f"{Path(root)}\0{session}"
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    _USERS[lock] = _USERS.get(lock, 0) + 1
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except TimeoutError:
            raise _TurnBusy from None
        try:
            yield
        finally:
            lock.release()
    finally:
        _USERS[lock] -= 1
        if not _USERS[lock] and locks.get(key) is lock:
            del locks[key]


async def _mark(synth: Any, state: ReflectionState, session: str, through: float) -> None:
    try:
        await asyncio.to_thread(state.mark, session, through)
    except OSError as exc:
        # Not marked: the chat's next close reads these turns again.
        synth.last_error = f"reflection state not saved: {exc}"


async def reflect(synth: Any) -> list[str]:
    """One reflection of this chat's new turns. Returns the change names.

    Runs on the chat's own route (provider row, model, effort), under the
    learner's daily budget and per-billing caps; a model the chat backs off
    is skipped. A session that failed the gate is marked reflected; one that
    could not be reflected (budget, backoff, provider error, busy) is not, so
    the next settle retries it, and so is one whose proposals the inbox could
    not store. Chats reflect concurrently; a second close of the same chat
    waits cooperatively for the first, then reads the session's watermark
    again before any call is paid for."""
    synth.last_pass_failed = False
    synth.last_pass_skipped = False
    store = synth._store
    session = getattr(synth, "_own_session", None) or getattr(synth, "_session_id", None)
    if not synth.enabled(refresh=True) or not session:
        synth.last_pass_skipped = True
        return []
    try:
        async with _chat_turn(store.root, str(session), REFLECTION_WAIT_SECONDS):
            return await _reflect_turn(synth, store, str(session))
    except _TurnBusy:
        synth.last_pass_skipped = True
        synth._log_pass("reflection", [], ran=False, counts_toward_yield=False,
                        extra={"session": str(session), "outcome": "skipped", "reason": "busy"})
        return []


async def _reflect_turn(synth: Any, store: Any, session: str) -> list[str]:
    from fluid.codemode.continual import (
        InferenceBudgetExhausted,
        SynthesisIncomplete,
        SynthesisProviderError,
    )

    state = ReflectionState(store.root)
    forgotten_before = store._facts.snapshot().get("forgotten_before", 0)
    through = state.through(session)
    entries = session_entries(store, session, through, forgotten_before)
    if not entries:
        advance_cursor(store, state)
        return []
    newest = max(float(e.get("ts") or 0) for e in entries)
    turns = sum(1 for e in entries if e.get("kind") == "user")
    base = {"session": session, "turns": turns}
    signals = gate(entries, continued=through > 0)
    if not signals:
        await _mark(synth, state, session, newest)
        synth.last_pass_skipped = True
        synth._log_pass("reflection", [], ran=False, counts_toward_yield=False,
                        extra={**base, "outcome": "skipped", "reason": "no_signal"})
        advance_cursor(store, state)
        return []
    synth._budget(refresh=True)
    synth._apply_target()
    synth._size_request()
    if synth._backing_off(synth.backoff_key()):
        synth.last_pass_skipped = True
        synth._log_pass("reflection", [], ran=False, counts_toward_yield=False,
                        extra={**base, "outcome": "skipped", "reason": "backoff", "signals": signals})
        return []
    projects = Counter(e.get("project") for e in entries if e.get("project"))
    original_project = synth._project
    if projects:
        synth._project = projects.most_common(1)[0][0]
    try:
        prompt = build_input(synth, entries, signals, synth._prompt_limit)
        try:
            text, usage = await synth._complete(prompt, REFLECT_SYSTEM, reserve=0)
        except InferenceBudgetExhausted:
            synth._latch_spent_day()
            synth._log_budget_spent("reflection")
            return []
        except (SynthesisIncomplete, SynthesisProviderError) as exc:
            synth.last_error = str(exc)
            retry = synth._note_failure(synth.last_error)
            synth._log_pass("reflection", [], error=synth.last_error, usage=exc.usage,
                            retry_after_s=retry, counts_toward_yield=False,
                            extra={**base, "outcome": "error", "signals": signals})
            return []
        if store._facts.snapshot().get("forgotten_before", 0) > forgotten_before:
            return []  # a Forget landed during the call: its text must not come back
        errors: list[str] = []
        inbox = ProposalInbox(store.root)
        changed, counts = await apply_output(synth, text, entries, session, inbox, errors)
    finally:
        synth._project = original_project
    synth.last_error = None
    if counts.get("unsaved"):
        # A proposal the inbox could not store would be lost for good if the
        # watermark moved: leave it, so the next close reflects these turns
        # again (rules and proposals already saved repeat without a copy).
        synth.last_pass_failed = True
    else:
        await _mark(synth, state, session, newest)
    try:
        store.clear_learner_backoff(synth.backoff_key())
    except Exception:  # noqa: BLE001
        pass
    if errors:
        synth.last_error = "; ".join(filter(None, [*errors, synth.last_error]))
    synth._log_pass("reflection", changed, error=synth.last_error, consumed=len(entries),
                    output_preview=text, usage=usage, stop_reason=synth.last_stop_reason,
                    counts_toward_yield=False,
                    extra={**base, "outcome": "ran", "signals": signals, **counts})
    advance_cursor(store, state)
    return changed

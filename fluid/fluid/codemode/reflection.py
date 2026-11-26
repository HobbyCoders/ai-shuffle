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
- **Where it goes.** A rule quoting the owner's words is saved at once as a
  learned behaviour rule (agent tier, revertible on Settings > Learning). A
  preference quoting the owner is saved at once as an agent-saved fact.
  Everything else is a *proposal* in :class:`ProposalInbox`: it becomes an
  agent-saved fact when a second, distinct session independently proposes it
  or when the owner accepts it, and otherwise expires after 14 days. An open
  proposal is not a fact: it is never in ``memories.json``, so it never takes
  a slot, never reaches the index, just-in-time delivery or Recall, and is
  never counted as a dropped fact.

The worker-wide rollback switch ``FLUID_MEMORY_LEARNER=continuous`` brings
back the continuous learner (skills, consolidation, when-line backfill and
workflow proposals included). Phase 3 deletes it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

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
PROPOSAL_TTL_SECONDS = 14 * 86400
MAX_PROPOSALS = 100
MAX_PROPOSAL_CHARS = 500
MAX_STATE_SESSIONS = 500
#: Word overlap at which two proposals of one kind and scope are the same.
MATCH_THRESHOLD = 0.6

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
OPEN, ACCEPTED, DISMISSED, PROMOTED = "open", "accepted", "dismissed", "promoted"
DECIDED = (ACCEPTED, DISMISSED, PROMOTED)
_DEFAULT_WHEN = {
    OWNER_PREFERENCE: "When working with the owner (learned preference)",
    OWNER_FACT: "When working with the owner",
    PROJECT: "When working in this project",
}


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


def _lock(root: Path, name: str):
    from fluid.codemode.skills import journal_lock

    return journal_lock(Path(root) / name, wait=5.0)


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
        try:
            return float(row.get("through", 0.0)) if isinstance(row, dict) else 0.0
        except (TypeError, ValueError):
            return 0.0

    def all_through(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for sid, row in self._sessions().items():
            try:
                out[sid] = float(row.get("through", 0.0))
            except (AttributeError, TypeError, ValueError):
                continue
        return out

    def mark(self, session: str, through: float, *, now: float | None = None) -> None:
        with _lock(self.path.parent, ".reflection-state"):
            sessions = self._sessions()
            sessions[session] = {"through": float(through), "at": float(now or time.time())}
            if len(sessions) > MAX_STATE_SESSIONS:
                newest = sorted(sessions.items(), key=lambda kv: -float(kv[1].get("at", 0) or 0))
                sessions = dict(newest[:MAX_STATE_SESSIONS])
            _write_json(self.path, {"schema": 1, "sessions": sessions})


# ── the proposal inbox ──────────────────────────────────────────────────────
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was "
    "were will with when owner user agent should always".split()
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    left, right = _words(a), _words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def proposal_id(kind: str, scope: str, text: str) -> str:
    basis = f"{kind}|{scope}|{' '.join(sorted(_words(text)))}"
    return "p" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:15]


def _valid_item(item: Any) -> bool:
    return (isinstance(item, dict) and isinstance(item.get("id"), str)
            and item.get("kind") in PROPOSAL_KINDS and isinstance(item.get("text"), str)
            and isinstance(item.get("sessions"), list))


class ProposalInbox:
    """Proposals reflection was not sure enough to save, kept in the owner's
    store directory, outside the fact store.

    ``proposals.json`` holds this worker's proposals and its decisions (an
    accepted, dismissed or promoted proposal stays listed for 14 days so the
    decision reaches other workers). ``proposals.remote.json`` holds the
    copies other workers published (app.network.memory_records): the inbox
    shows the merged view, and a proposal another worker's session produced
    counts toward corroboration here. Phase 3 moves this to SQLite."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / PROPOSAL_FILE
        self.remote_path = self.root / REMOTE_PROPOSAL_FILE

    # storage
    def _local(self) -> list[dict]:
        doc = _read_json(self.path)
        items = doc.get("proposals") if isinstance(doc, dict) else None
        return [i for i in items if _valid_item(i)] if isinstance(items, list) else []

    def _remote(self) -> dict[str, list[dict]]:
        doc = _read_json(self.remote_path)
        devices = doc.get("devices") if isinstance(doc, dict) else None
        if not isinstance(devices, dict):
            return {}
        out: dict[str, list[dict]] = {}
        for device, items in devices.items():
            if isinstance(items, list):
                out[str(device)] = [i for i in items if _valid_item(i)]
        return out

    def _save(self, items: list[dict], now: float) -> None:
        _write_json(self.path, {"schema": 1, "proposals": _prune(items, now)})

    def _announce(self) -> None:
        """Tell the host this worker's proposals changed (it republishes
        them; see app.network.memory_records). After the lock is released."""
        from fluid.codemode import transactions

        transactions.committed_change(self.path)

    def export(self, now: float | None = None) -> list[dict]:
        """This worker's proposals and decisions, for replication."""
        return _prune(self._local(), now or time.time())

    def apply_remote(self, device: str, items: list[dict] | None) -> None:
        """Keep another worker's published proposals (None: it has none)."""
        with _lock(self.root, ".proposals"):
            doc = _read_json(self.remote_path)
            devices = doc.get("devices") if isinstance(doc, dict) else None
            devices = devices if isinstance(devices, dict) else {}
            clean = [i for i in (items or []) if _valid_item(i)][:MAX_PROPOSALS]
            if clean:
                devices[str(device)] = clean
            else:
                devices.pop(str(device), None)
            _write_json(self.remote_path, {"schema": 1, "devices": devices})

    # the merged view
    def _merged(self, now: float) -> dict[str, dict]:
        """Every proposal by id over this worker and the others: sessions
        joined, decided if any worker decided it, expiring at the latest."""
        merged: dict[str, dict] = {}
        sources = [("local", self._local())] + [(d, items) for d, items in self._remote().items()]
        for origin, items in sources:
            for item in items:
                row = merged.get(item["id"])
                if row is None:
                    row = merged[item["id"]] = {**item, "sessions": [], "origins": [],
                                                "status": OPEN, "expires_at": 0.0}
                for sid in item.get("sessions") or []:
                    if isinstance(sid, str) and sid not in row["sessions"]:
                        row["sessions"].append(sid)
                row["origins"].append(origin)
                if item.get("status") in DECIDED:
                    row["status"] = item["status"]
                    row["decided_at"] = item.get("decided_at")
                row["expires_at"] = max(float(row["expires_at"] or 0.0), float(item.get("expires_at") or 0.0))
                row["created_at"] = min(float(row.get("created_at") or now), float(item.get("created_at") or now))
                if not row.get("quote") and item.get("quote"):
                    row["quote"] = item["quote"]
        return merged

    def open(self, now: float | None = None) -> list[dict]:
        """Open, unexpired proposals, newest first."""
        now = now or time.time()
        rows = [r for r in self._merged(now).values()
                if r["status"] == OPEN and r["expires_at"] > now]
        rows.sort(key=lambda r: -float(r.get("updated_at") or r.get("created_at") or 0))
        return rows

    def get(self, pid: str, now: float | None = None) -> dict | None:
        return next((r for r in self.open(now) if r["id"] == pid), None)

    def _match(self, kind: str, scope: str, text: str, now: float) -> dict | None:
        best, score = None, 0.0
        for row in self.open(now):
            if row["kind"] != kind or row.get("scope") != scope:
                continue
            s = similarity(text, row["text"])
            if s > score:
                best, score = row, s
        return best if score >= MATCH_THRESHOLD else None

    # writes
    def propose(self, *, kind: str, text: str, scope: str, session: str,
                quote: str | None = None, when: str | None = None,
                now: float | None = None) -> tuple[dict, bool]:
        """Add a proposal, or this session to a matching one. Returns the
        merged proposal and whether it is now corroborated (two distinct
        sessions). The caller saves a corroborated one as a fact and then
        :meth:`decide` s it ``promoted``."""
        if kind not in PROPOSAL_KINDS:
            raise ValueError(f"unknown proposal kind {kind!r}")
        now = now or time.time()
        with _lock(self.root, ".proposals"):
            match = self._match(kind, scope, text, now)
            items = self._local()
            pid = match["id"] if match else proposal_id(kind, scope, text)
            mine = next((i for i in items if i["id"] == pid), None)
            if mine is None:
                base = match or {"text": text, "when": when, "quote": quote,
                                 "created_at": now, "expires_at": now + PROPOSAL_TTL_SECONDS}
                mine = {"id": pid, "kind": kind, "scope": scope, "text": base["text"],
                        "when": base.get("when"), "quote": base.get("quote") or quote,
                        "sessions": [], "created_at": float(base.get("created_at") or now),
                        "expires_at": float(base.get("expires_at") or now + PROPOSAL_TTL_SECONDS),
                        "status": OPEN}
                items.append(mine)
            if session not in mine["sessions"]:
                mine["sessions"].append(session)
            if not mine.get("quote") and quote:
                mine["quote"] = quote
            mine["updated_at"] = now
            self._save(items, now)
        self._announce()
        merged = self._merged(now).get(pid) or mine
        return merged, len(set(merged["sessions"])) >= 2 and merged["status"] == OPEN

    def decide(self, pid: str, status: str, *, fact_id: str | None = None,
               now: float | None = None) -> dict:
        """Record the owner's (or corroboration's) decision on an open
        proposal. KeyError when it is not open here."""
        if status not in DECIDED:
            raise ValueError(f"unknown decision {status!r}")
        now = now or time.time()
        with _lock(self.root, ".proposals"):
            row = self._merged(now).get(pid)
            if row is None or row["status"] != OPEN or row["expires_at"] <= now:
                raise KeyError(pid)
            items = self._local()
            mine = next((i for i in items if i["id"] == pid), None)
            if mine is None:
                mine = {k: row.get(k) for k in ("id", "kind", "scope", "text", "when", "quote",
                                                  "created_at", "expires_at")}
                mine["sessions"] = list(row["sessions"])
                items.append(mine)
            mine.update(status=status, decided_at=now, updated_at=now)
            if fact_id:
                mine["fact_id"] = fact_id
            self._save(items, now)
        self._announce()
        return {**row, "status": status}


def _prune(items: list[dict], now: float) -> list[dict]:
    """Drop expired open proposals and decisions older than the TTL; keep at
    most :data:`MAX_PROPOSALS`, decisions before the oldest open ones."""
    kept = []
    for item in items:
        if item.get("status") in DECIDED:
            if float(item.get("decided_at") or 0) + PROPOSAL_TTL_SECONDS > now:
                kept.append(item)
        elif float(item.get("expires_at") or 0) > now:
            kept.append(item)
    if len(kept) > MAX_PROPOSALS:
        kept.sort(key=lambda i: (i.get("status") == OPEN, -float(i.get("updated_at") or 0)))
        kept = kept[:MAX_PROPOSALS]
    return kept


def promote(store: Any, proposal: dict, *, session: str | None, automatic: bool) -> Any:
    """Save ``proposal`` as an agent-saved fact (durable tier)."""
    from fluid.codemode import facts

    when = proposal.get("when") or _DEFAULT_WHEN.get(proposal.get("kind"), _DEFAULT_WHEN[OWNER_FACT])
    try:
        when = facts.validate_when(when)
    except ValueError:
        when = _DEFAULT_WHEN[OWNER_FACT]
    return store.save_memory(
        proposal["text"], scope=proposal.get("scope") or "global",
        provenance="user" if proposal.get("quote") else "session",
        durable=True, source_session=session or (proposal.get("sessions") or [None])[0],
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
- a behaviour rule, when the owner corrected HOW the agent works: one line,
  "When <situation>: <what to do>", imperative, at most 300 characters,
  general enough for next time but keeping the owner's scope and exceptions.
  quote= must be copied word for word from the [user] line that shows the
  correction; a rule without a real quote is refused. applies_to is "all"
  unless the failure is specific to the model family that line was talking
  to ("[user to <family>]"). Prefer rewriting a listed rule (its id) to a
  near-duplicate. A rule never grants permission ("without asking", "skip
  review").
  <rule applies_to="all" quote="exact owner words">When X: do Y.</rule>
  <rule id="m7" applies_to="all" quote="exact owner words">When X: do Y.</rule>
- a proposal, for something TRUE worth remembering, one fact per item, at
  most 500 characters:
  <proposal kind="KIND" scope="global|project" quote="exact owner words" when="When ...">the fact</proposal>
  KIND is owner_preference (how the owner likes things done, in their own
  words: quote required), owner_fact (about the owner, their machines or
  accounts) or project (this project's code, layout or conventions:
  scope="project"). quote is optional except for owner_preference; when= is
  one short line saying when the fact matters.
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


def apply_output(synth: Any, text: str, entries: list[dict], session: str,
                 inbox: ProposalInbox, errors: list[str]) -> tuple[list[str], dict[str, int]]:
    """Save the rules and quoted preferences, queue the other proposals and
    promote the ones a second session corroborated. Returns the change names
    and counts by outcome."""
    from fluid.codemode import behavior, facts

    changed = list(synth._apply_rules(text, entries, errors))
    counts = {"rules": len(changed), "saved": 0, "proposed": 0, "corroborated": 0}
    store = synth._store
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
        quoted = bool(quote) and behavior.quote_in_user_text(quote, entries)
        if quote and not quoted:
            quote = None
        scope_attr = attrs.get("scope") or ("project" if kind == PROJECT else "global")
        scope = synth._resolve_scope(scope_attr) or "global"
        if kind == OWNER_PREFERENCE and quoted:
            try:
                saved = store.save_memory(
                    body, scope=scope, provenance="user", durable=True,
                    source_session=session, when=when or _DEFAULT_WHEN[OWNER_PREFERENCE],
                    automatic=True,
                )
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
                continue
            changed.append(f"memory:{saved.id}")
            counts["saved"] += 1
            continue
        try:
            row, corroborated = inbox.propose(kind=kind, text=body, scope=scope, session=session,
                                              quote=quote, when=when)
        except (OSError, ValueError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        if row.get("status") != OPEN:
            continue  # decided (dismissed) within the last 14 days: stays decided
        if not corroborated:
            changed.append(f"proposal:{row['id']}")
            counts["proposed"] += 1
            continue
        try:
            saved = promote(store, row, session=session, automatic=True)
            inbox.decide(row["id"], PROMOTED, fact_id=saved.id)
        except (KeyError, OSError, ValueError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        changed.append(f"memory:{saved.id}")
        counts["corroborated"] += 1
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


async def reflect(synth: Any) -> list[str]:
    """One reflection of this chat's new turns. Returns the change names.

    Runs on the chat's own route (provider row, model, effort), under the
    learner's daily budget and per-billing caps; a model the chat backs off
    is skipped. A session that failed the gate is marked reflected; one that
    could not be reflected (budget, backoff, provider error) is not, so the
    next settle retries it."""
    from fluid.codemode.continual import (
        SYNTHESIS_LOCK,
        InferenceBudgetExhausted,
        SynthesisIncomplete,
        SynthesisProviderError,
    )
    from fluid.codemode.skills import journal_lock

    synth.last_pass_failed = False
    synth.last_pass_skipped = False
    store = synth._store
    session = getattr(synth, "_own_session", None) or getattr(synth, "_session_id", None)
    if not synth.enabled(refresh=True) or not session:
        synth.last_pass_skipped = True
        return []
    state = ReflectionState(store.root)
    forgotten_before = store._facts.snapshot().get("forgotten_before", 0)
    through = state.through(str(session))
    entries = session_entries(store, str(session), through, forgotten_before)
    if not entries:
        advance_cursor(store, state)
        return []
    newest = max(float(e.get("ts") or 0) for e in entries)
    turns = sum(1 for e in entries if e.get("kind") == "user")
    base = {"session": str(session), "turns": turns}
    signals = gate(entries, continued=through > 0)
    if not signals:
        state.mark(str(session), newest)
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
    with journal_lock(store.root / SYNTHESIS_LOCK, wait=30.0) as held:
        if not held:
            synth.last_pass_skipped = True
            synth._log_pass("reflection", [], ran=False, counts_toward_yield=False,
                            extra={**base, "outcome": "skipped", "reason": "busy", "signals": signals})
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
            changed, counts = apply_output(synth, text, entries, str(session), inbox, errors)
        finally:
            synth._project = original_project
    state.mark(str(session), newest)
    try:
        store.clear_learner_backoff(synth.backoff_key())
    except Exception:  # noqa: BLE001
        pass
    synth.last_error = "; ".join(errors) if errors else None
    synth._log_pass("reflection", changed, error=synth.last_error, consumed=len(entries),
                    output_preview=text, usage=usage, stop_reason=synth.last_stop_reason,
                    counts_toward_yield=False,
                    extra={**base, "outcome": "ran", "signals": signals, **counts})
    advance_cursor(store, state)
    return changed

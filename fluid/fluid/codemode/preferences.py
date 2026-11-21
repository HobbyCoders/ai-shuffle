"""Standing-preference proposals for user-kept facts.

The background learner may not write a fact the owner asked to keep
(``protected``), yet those are the facts that go stale when the owner changes
how they want the work done. This module lets the learner raise the question,
bounded so it cannot nag. (Two saved rules that contradict each other are a
separate question: see :mod:`fluid.codemode.conflict_review`.)

:class:`PreferenceProposals`: when a synthesis pass sees the user correct
HOW the agent works (model choice, delegation, reviews, CI, deploys,
process) it proposes the whole current rule under a short snake_case
topic name. The proposal is queued in ``pref-proposals.jsonl`` beside the store, never
saved as a fact, and the next sessions' ``# Memory`` snapshot shows it as
one line asking the agent to confirm it with the owner. At most
:data:`MAX_OPEN_PREFERENCE_PROPOSALS` are open, one per topic (a newer
wording replaces the older), and each expires after
:data:`PREFERENCE_PROPOSAL_TTL`. A proposal closes exactly when a tool
call names its topic: a successful Remember with ``proposal=<topic>``
(the rule was saved) or Forget with ``proposal=<topic>`` and
``confirm=true`` (the owner declined it, or a saved memory already states
it). Either closes every open proposal on that topic the calling session
sees (its project's or global; :meth:`PreferenceProposals.close`), and
the closed row stays as a tombstone for a TTL after closing so the
learner does not queue the same topic, scope and wording again. A
different wording is still queued. Tombstones are never open. The
queue file is local to this worker; it does not replicate. A proposal
also closes itself once a user-kept memory on the same topic is saved
after it: one keyed by the topic name, or one sharing the proposal's
subject terms (:func:`same_topic`); a memory the proposal already saw
when it was queued never counts, whatever its timestamp. The guidance queue
(``pending-proposals.jsonl``) is not reused: ``fluid evolve`` replays
everything in it through the guidance gate.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

PREFERENCE_PROPOSALS_FILE = "pref-proposals.jsonl"
MAX_OPEN_PREFERENCE_PROPOSALS = 3
PREFERENCE_PROPOSAL_TTL = 14 * 86400.0
# Topics older versions wrote as ``pref.<topic>`` keys are still read.
_LEGACY_KEY_PREFIX = "pref."
# A user-kept memory saved after a proposal answers it when it shares at
# least this many subject terms with the proposal, and they are at least
# this share of the smaller side's terms.
PROPOSAL_ANSWER_MIN_SHARED = 3
PROPOSAL_ANSWER_MIN_RATIO = 0.5

_TOPIC_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")


def normalize_topic(raw: Any) -> str | None:
    """``subagent_models`` from ``subagent_models``, a legacy
    ``pref.subagent_models`` or ``Subagent-Models``; None when it is not a
    short snake_case name."""
    topic = str(raw or "").strip().casefold()
    if topic.startswith(_LEGACY_KEY_PREFIX):
        topic = topic[len(_LEGACY_KEY_PREFIX):]
    topic = re.sub(r"[\s\-.]+", "_", topic)
    return topic if _TOPIC_RE.fullmatch(topic) else None


def _fingerprint(text: str) -> str:
    from fluid.codemode.facts import fingerprint
    return fingerprint(text)


def _scope_visible(scope: str, project: str | None) -> bool:
    return project is None or scope in ("global", project)


def _closed(record: dict) -> bool:
    """A tombstone: answered by a tool call naming its topic. Rows older
    versions wrote carry no ``closed_at`` and stay ordinary proposals."""
    return isinstance(record.get("closed_at"), (int, float)) and not isinstance(record["closed_at"], bool)


def _live_tombstone(record: dict, now: float) -> bool:
    return _closed(record) and now - float(record["closed_at"]) < PREFERENCE_PROPOSAL_TTL


def same_topic(topic: str, text: str, key: str | None, terms: set[str]) -> bool:
    """Whether a saved memory (``key``, subject ``terms``) is on the topic of
    a proposal (``topic``, ``text``): keyed by the topic name (or a legacy
    ``pref.`` key), or sharing enough subject terms (:func:`_salient`)."""
    name = normalize_topic(topic)
    if key and normalize_topic(key.rsplit(".", 1)[-1] if key.startswith(_LEGACY_KEY_PREFIX) else key) == name:
        return True
    mine = _salient(text)
    shared = len(mine & terms)
    return (shared >= PROPOSAL_ANSWER_MIN_SHARED
            and shared >= PROPOSAL_ANSWER_MIN_RATIO * max(1, min(len(mine), len(terms))))


class PreferenceProposals:
    """The bounded queue of proposed rule updates for one store."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self.path = Path(store.root) / PREFERENCE_PROPOSALS_FILE
        self._lock = Path(store.root) / "pref-proposals"

    # ─── reading ────────────────────────────────────────────────────────
    def _read(self) -> list[dict]:
        records: list[dict] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return records
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (isinstance(record, dict) and normalize_topic(record.get("topic"))
                    and isinstance(record.get("text"), str)
                    and isinstance(record.get("ts"), (int, float))):
                records.append(record)
        return records

    def _saved_preferences(self, entries: list[Any] | None) -> list[tuple[str, str, float, str, set[str]]]:
        """``(key, scope, updated_at, fingerprint, subject terms)`` of live
        user-kept facts (dicts or MemoryEntry objects)."""
        if entries is None:
            try:
                entries = self._store.memories()
            except Exception:  # noqa: BLE001 — unreadable store: nothing resolves
                entries = []
        from fluid.codemode.facts import is_superseded
        rows = [entry if isinstance(entry, dict) else vars(entry) for entry in entries]
        out = []
        for entry in rows:
            get = entry.get
            if (get("protected") and not is_superseded(entry) and not get("conflicted")
                    and not get("retired_at")):
                text = str(get("text") or "")
                out.append((str(get("key") or ""), str(get("scope") or "global"),
                            float(get("updated_at") or get("created_at") or 0.0),
                            _fingerprint(text), _salient(text)))
        return out

    def _is_open(self, record: dict, saved, now: float) -> bool:
        if _closed(record) or now - float(record["ts"]) >= PREFERENCE_PROPOSAL_TTL:
            return False
        scope = record.get("scope") or "global"
        # Answered: the owner's confirmation (or correction) of the topic was
        # saved after the proposal was made. ``ts`` is this worker's clock;
        # ``seen`` is the newest memory on the topic the proposal already saw,
        # which never answers it even when replicated from a clock that runs
        # ahead (rows from older versions carry no ``seen``).
        seen = record.get("seen")
        seen = float(seen) if isinstance(seen, (int, float)) and not isinstance(seen, bool) else None
        return not any((s == scope or s == "global") and at >= float(record["ts"])
                       and (seen is None or at > seen)
                       and same_topic(record["topic"], record["text"], k, terms)
                       for k, s, at, _, terms in saved)

    def open(self, project: str | None = None, *, entries: list[Any] | None = None,
             now: float | None = None) -> list[dict]:
        """Open proposals a session in ``project`` should see, oldest first.
        ``project=None`` lists every scope."""
        now = time.time() if now is None else now
        saved = self._saved_preferences(entries)
        return [r for r in self._read()
                if self._is_open(r, saved, now) and _scope_visible(r.get("scope") or "global", project)]

    # ─── writing ────────────────────────────────────────────────────────
    def propose(self, topic: Any, text: str, *, scope: str = "global",
                sessions: list[str] | tuple[str, ...] = (), now: float | None = None) -> str:
        """Queue a proposed rule for ``topic``.

        Returns ``proposed`` (queued, or replaced the topic's older wording),
        ``duplicate`` (the same wording is already open), ``closed`` (a
        session already saved or dismissed this topic, scope and wording;
        see :meth:`close`), ``saved`` (a user-kept fact in scope already
        says it word for word, and no newer user-kept fact in scope on the
        same topic says otherwise) or ``full`` (the queue holds
        MAX_OPEN_PREFERENCE_PROPOSALS other topics). Raises
        ValueError for a malformed topic, an empty or oversized text, a
        credential or an injection pattern."""
        from fluid.codemode.facts import validate_text
        from fluid.codemode.skills import atomic_write_text, journal_lock
        from fluid.context.screening import screen_findings

        name = normalize_topic(topic)
        if name is None:
            raise ValueError(f"preference topic {str(topic)[:40]!r} is not a short snake_case name")
        body = validate_text(text)
        if screen_findings(body):
            raise ValueError("preference proposal matches an injection pattern")
        scope = scope or "global"
        now = time.time() if now is None else now
        fingerprint = _fingerprint(body)
        with journal_lock(self._lock, wait=1.0) as held:
            if not held:
                raise ValueError("preference proposals are busy; the next pass retries")
            saved = self._saved_preferences(None)
            in_scope = [row for row in saved if row[1] in (scope, "global")]
            same_words = [at for _, _, at, fp, _ in in_scope if fp == fingerprint]
            on_topic = [(at, fp) for k, _, at, fp, terms in in_scope if same_topic(name, body, k, terms)]
            # A word-for-word memory answers the proposal only while it is the
            # latest word on the topic: a newer live user-kept memory on the
            # same topic that says something else leaves it open (#1508).
            if same_words and not any(fp != fingerprint and at > max(same_words)
                                      for at, fp in on_topic):
                return "saved"
            rows = self._read()
            records = [r for r in rows if self._is_open(r, saved, now)]
            tombstones = [r for r in rows if _live_tombstone(r, now)]
            same = [r for r in records if normalize_topic(r["topic"]) == name
                    and (r.get("scope") or "global") == scope]
            if any(_fingerprint(r["text"]) == fingerprint for r in same):
                return "duplicate"
            if any(normalize_topic(r["topic"]) == name and (r.get("scope") or "global") == scope
                   and _fingerprint(r["text"]) == fingerprint for r in tombstones):
                return "closed"
            records = [r for r in records if r not in same]
            if not same and len(records) >= MAX_OPEN_PREFERENCE_PROPOSALS:
                return "full"
            record = {"ts": now, "topic": name, "text": body, "scope": scope,
                      "sessions": [str(s) for s in sessions if s][:4]}
            if on_topic:
                record["seen"] = max(at for at, _ in on_topic)
            records.append(record)
            self._write(atomic_write_text, tombstones + records)
        return "proposed"

    def close(self, topic: Any, project: str | None, *, how: str,
              now: float | None = None) -> list[dict]:
        """Close every unexpired, not yet closed proposal on ``topic`` that a
        session in ``project`` sees (scope ``global`` or ``project``; None
        sees only global) and return them. Each stays as a tombstone
        (``closed_at``, ``closed=how``) for PREFERENCE_PROPOSAL_TTL, which
        :meth:`propose` reads to skip the same wording. Raises ValueError for
        a malformed topic and TimeoutError when the queue lock stays busy;
        nothing changes then."""
        from fluid.codemode.skills import atomic_write_text, journal_lock
        name = normalize_topic(topic)
        if name is None:
            raise ValueError(f"proposal {str(topic)[:40]!r} is not a short snake_case topic")
        now = time.time() if now is None else now
        with journal_lock(self._lock, wait=1.0) as held:
            if not held:
                raise TimeoutError("preference proposals are busy")
            rows = self._read()
            closed = [r for r in rows if not _closed(r)
                      and now - float(r["ts"]) < PREFERENCE_PROPOSAL_TTL
                      and normalize_topic(r["topic"]) == name
                      and (r.get("scope") or "global") in ("global", project)]
            for r in closed:
                r["closed_at"], r["closed"] = now, how
            if closed:
                self._write(atomic_write_text, rows)
        return closed

    def _write(self, atomic_write_text, rows: list[dict]) -> None:
        atomic_write_text(self.path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def preference_proposal_lines(store: Any, project: str | None = None, *,
                              entries: list[Any] | None = None) -> list[str]:
    """One snapshot line per open proposal (never raises)."""
    try:
        records = PreferenceProposals(store).open(project, entries=entries)
    except Exception:  # noqa: BLE001 — a proposal must never break the prompt
        return []
    lines = []
    for record in records[:MAX_OPEN_PREFERENCE_PROPOSALS]:
        topic = normalize_topic(record["topic"])
        lines.append(
            f"- [proposed, not saved; {topic}] {record['text']} (From the owner's recent "
            f"corrections. Confirm it with the owner, then save it with Remember, proposal='{topic}', "
            f"correcting the memory on this topic (replaces=[older rows]); if the owner's current "
            f"message already says it, save it without asking. If the owner declines or a saved "
            f"memory already states it: Forget, proposal='{topic}', confirm=true.)"
        )
    return lines


def valid_proposal(raw: Any) -> str | None:
    """The topic a tool call's ``proposal`` names: "" when absent, None when
    malformed (the call must then change nothing)."""
    if raw is None or raw == "":
        return ""
    return normalize_topic(raw) if isinstance(raw, str) else None


def close_proposal(store: Any, topic: str, project: str | None, *, how: str) -> str:
    """Close the proposals ``topic`` names for a session in ``project`` and
    describe it for the tool receipt. Raises TimeoutError/OSError when the
    queue cannot be written; the caller decides what that means."""
    closed = PreferenceProposals(store).close(topic, project, how=how)
    if not closed:
        return f"No open learner proposal has topic '{topic}'; nothing was closed."
    rows = "; ".join(f"({r.get('scope') or 'global'}) {_clip(r['text']).rstrip('. ')}" for r in closed)
    return (f"Closed learner proposal '{topic}' ({how}): {rows}. For "
            f"{PREFERENCE_PROPOSAL_TTL // 86400:.0f} days the learner will not propose the same "
            "wording again.")


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# Words that say nothing about which rule a fact states.
_NOISE = frozenset("""
owner user agent agents rule rules always never only still not must should default
use uses used using via per each every new old now when then also any all
""".split())


def _salient(text: str) -> set[str]:
    from fluid.codemode.supersession import subject_terms
    return {t for t in subject_terms(text) if t not in _NOISE and not t.isdigit()}

"""Standing-preference proposals and conflict curation for user-kept facts.

The background learner may not write a fact the owner asked to keep
(``protected``), yet those are the facts that go stale when the owner changes
how they want the work done. This module gives the learner two ways to
raise the question, both bounded so they cannot nag:

* :class:`PreferenceProposals`: when a synthesis pass sees the user correct
  HOW the agent works (model choice, delegation, reviews, CI, deploys,
  process) it proposes the whole current rule under a short snake_case
  topic name. The proposal is queued in ``pref-proposals.jsonl`` beside the store, never
  saved as a fact, and the next sessions' ``# Memory`` snapshot shows it as
  one line asking the agent to confirm it with the owner. At most
  :data:`MAX_OPEN_PREFERENCE_PROPOSALS` are open, one per topic (a newer
  wording replaces the older), and each expires after
  :data:`PREFERENCE_PROPOSAL_TTL`. A proposal closes itself once a user-kept
  memory on the same topic is saved after it: one keyed by the topic name,
  or one sharing the proposal's subject terms (:func:`same_topic`). No key
  convention is needed. The guidance queue
  (``pending-proposals.jsonl``) is not reused: ``fluid evolve`` replays
  everything in it through the guidance gate.

* :func:`curate_protected_conflicts`: deterministic, model-free detection of
  two live user-kept facts about the same how-the-agent-works topic where
  the newer one changes the rule. A non-link question in the existing
  memory footer shows both texts and asks the owner to resolve the pair.
  Both facts remain usable as context and model-routing authority; agents
  ask which applies rather than silently choose for the contested job.
  Curation never links, archives or rewrites a user-kept fact. At most
  :data:`MAX_OPEN_CONFLICTS` questions are shown at once. An owner-kept
  Remember decision under the question's version-bound key records that
  both stand, replicates like any owner fact, and closes that pair until
  either text (or its applicability) changes. No tool schema is extended.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from fluid.codemode.routing import affirmed_clauses

PREFERENCE_PROPOSALS_FILE = "pref-proposals.jsonl"
CONFLICT_CURATION_FILE = "conflict-curation.json"
MAX_OPEN_PREFERENCE_PROPOSALS = 3
PREFERENCE_PROPOSAL_TTL = 14 * 86400.0
# Topics older versions wrote as ``pref.<topic>`` keys are still read.
_LEGACY_KEY_PREFIX = "pref."
# A user-kept memory saved after a proposal answers it when it shares at
# least this many subject terms with the proposal, and they are at least
# this share of the smaller side's terms.
PROPOSAL_ANSWER_MIN_SHARED = 3
PROPOSAL_ANSWER_MIN_RATIO = 0.5
MAX_OPEN_CONFLICTS = 3
# Diagnostic pairs already curated (newest kept), not owner answers. This
# suppresses repeat curation work only; replicated owner facts close questions.
MAX_REMEMBERED_PAIRS = 128

_TOPIC_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")
_WORD_RE = re.compile(r"[a-z0-9]+")


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
        if now - float(record["ts"]) >= PREFERENCE_PROPOSAL_TTL:
            return False
        scope = record.get("scope") or "global"
        # Answered: the owner's confirmation (or correction) of the topic was
        # saved after the proposal was made.
        return not any((s == scope or s == "global") and at >= float(record["ts"])
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
        ``duplicate`` (the same wording is already open), ``saved`` (a
        user-kept fact in scope already says it word for word) or ``full`` (the
        queue holds MAX_OPEN_PREFERENCE_PROPOSALS other topics). Raises
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
            if any(s in (scope, "global") and fp == fingerprint for _, s, _, fp, _ in saved):
                return "saved"
            records = [r for r in self._read() if self._is_open(r, saved, now)]
            same = [r for r in records if normalize_topic(r["topic"]) == name
                    and (r.get("scope") or "global") == scope]
            if any(_fingerprint(r["text"]) == fingerprint for r in same):
                return "duplicate"
            records = [r for r in records if r not in same]
            if not same and len(records) >= MAX_OPEN_PREFERENCE_PROPOSALS:
                return "full"
            records.append({"ts": now, "topic": name, "text": body, "scope": scope,
                            "sessions": [str(s) for s in sessions if s][:4]})
            atomic_write_text(self.path, "".join(
                json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        return "proposed"

    def dismiss(self, topic: Any, *, scope: str | None = None) -> bool:
        """Drop the open proposal for ``topic`` (any scope unless given)."""
        from fluid.codemode.skills import atomic_write_text, journal_lock
        name = normalize_topic(topic)
        with journal_lock(self._lock, wait=1.0) as held:
            if not held or name is None:
                return False
            records = self._read()
            kept = [r for r in records if not (normalize_topic(r["topic"]) == name
                                               and (scope is None or (r.get("scope") or "global") == scope))]
            if len(kept) == len(records):
                return False
            atomic_write_text(self.path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
            return True


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
            f"- [proposed, not saved; {topic}] {record['text']} (The learner read this in the "
            f"owner's recent corrections. Confirm it with the owner, then save it with Remember, "
            f"correcting the memory on this topic (replaces=[older rows]); if the owner's current "
            f"message already says it, save it without asking.)"
        )
    return lines


# ─── conflict curation for user-kept facts ─────────────────────────────────

# How-the-agent-works topics a contradiction is looked for in.
#
# Model routing is read clause by clause (``;``, a sentence end or a line
# break ends one) into a job -> models map: "code work goes to Sol; reviews
# go to Opus" is {code: sol, review: opus}. The newer of two routing facts
# changes the rule when a job both name maps to different models; one that
# only adds jobs, or names the same models for the jobs both cover, does not.
_MODEL_NAMES = frozenset(
    "opus sonnet haiku fable mythos sol astra luna terra gpt claude codex grok glm gemini qwen".split())
# Family words a clause names beside a specific model ("GPT 6.1 Sol").
_MODEL_FAMILIES = frozenset({"gpt", "claude"})
_JOB_WORDS: dict[str, frozenset[str]] = {
    "code": frozenset("code coding coder implementation implement".split()),
    "review": frozenset("review reviews reviewer reviewing".split()),
    "frontend": frozenset("frontend ui".split()),
    "backend": frozenset("backend".split()),
    "docs": frozenset("docs documentation".split()),
}
# Use the authority path's exact negation/list/contrast screen. Keep the
# sentence's subject beside its positive parts ("backend: Sol"), then require
# an actual assignment, not an anecdote that merely names a model and a job.
_CLAUSE_RE = re.compile(r"[;\n—–]|[.!?](?=\s|$)")
# Noun phrases also assign models; exclude historical experiments instead of
# restricting the vocabulary used to express an owner rule.
_PAST_ROUTING_EVENT = re.compile(
    r"\b(?:tried|tested|experimented|evaluated|used|ran|previously|formerly|yesterday)\b"
    r"|\blast\s+(?:week|month|year|time|night)\b|\b\d+\s+\w+\s+ago\b", re.I)
_CURRENT_BREAK = re.compile(r"\b(?:but|and)\s+(?=(?:now|currently|from\s+now|use|switch|move)\b)", re.I)


def model_routing(text: str) -> dict[str, frozenset[str]]:
    """Positive routing statements only; mentioning a past model is not one."""
    routes: dict[str, frozenset[str]] = {}
    clauses = (part for sentence in _CLAUSE_RE.split((text or "").casefold())
               for part in _CURRENT_BREAK.split(sentence))
    for clause in clauses:
        if _PAST_ROUTING_EVENT.search(clause):
            continue
        positive = " ".join(affirmed_clauses(clause))
        # "code review" is a review, not code work.
        words = _words(re.sub(r"\bcode[\s-]+review", "review", positive))
        models = words & _MODEL_NAMES
        models = (models - _MODEL_FAMILIES) or models
        if not models:
            continue
        for job, cues in _JOB_WORDS.items():
            if words & cues:
                routes[job] = routes.get(job, frozenset()) | models
    return routes


# The other topics are value sets: a fact is on one when its words hit
# ``cues`` of the cue words and ``values`` of the value words, and the newer
# fact changes the rule when the value words differ (CI moved to another
# machine, a deploy that now needs no approval).
PREFERENCE_TOPICS: dict[str, dict[str, Any]] = {
    "ci_policy": {
        "cue_words": frozenset("ci suite suites runner runners prepush pre push pytest tests testing".split()),
        "value_words": frozenset("github local locally machine machines worker workers hosted paid larger nightly".split()),
        "cues": 2, "values": 1,
    },
    "deploy": {
        "cue_words": frozenset("deploy deploys deployed deploying deployment deployments".split()),
        "value_words": frozenset("ask asking approval approve approved without dispatch dispatched "
                                 "owner manual automatic automatically".split()),
        "cues": 1, "values": 1,
    },
}
# Key namespaces of facts about who the owner is, not how work is done (the
# snapshot's identity tier); a career history naming a model is no rule.
_BIOGRAPHY_KEYS = ("profile.", "career.", "business.")
# Words that say nothing about which rule a fact states.
_NOISE = frozenset("""
owner user agent agents rule rules always never only still not must should default
use uses used using via per each every new old now when then also any all
""".split())


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").casefold()))


def preference_topics(text: str) -> dict[str, frozenset[str]]:
    """``{topic: value words named}`` for every topic ``text`` is on."""
    words = _words(text)
    out = {}
    for topic, spec in PREFERENCE_TOPICS.items():
        cues = words & spec["cue_words"]
        values = words & spec["value_words"]
        if len(cues) >= spec["cues"] and len(values) >= spec["values"]:
            out[topic] = frozenset(values)
    return out


def _salient(text: str) -> set[str]:
    from fluid.codemode.supersession import subject_terms
    return {t for t in subject_terms(text) if t not in _NOISE and not t.isdigit()}


def _pair_key(old: dict, new: dict) -> str:
    from fluid.codemode.facts import global_id
    return f"{global_id(old)}>{global_id(new)}"


# A decision is an ordinary owner-kept fact, not a new local-only queue.
# Its key includes both global IDs and the exact text/applicability the owner
# answered about. Recall/Remember/Forget and replication work unchanged.
COEXIST_KEY_PREFIX = "memory.coexist."


def coexistence_key(old: dict, new: dict) -> str:
    # Version 2 names independently hashed endpoints: answer validation is O(n),
    # not a hash of every possible pair. The old opaque pair keys stay readable.
    tokens = sorted(_coexistence_token(e) for e in (old, new))
    return COEXIST_KEY_PREFIX + "v2." + ".".join(tokens)


def _coexistence_value(e: dict) -> tuple:
    from fluid.codemode.facts import global_id
    return (global_id(e), e["text"], e.get("when"), bool(e.get("always")),
            e.get("scope") or "global", e.get("worker"))


def _coexistence_token(e: dict) -> str:
    return hashlib.sha256(json.dumps(_coexistence_value(e), ensure_ascii=False).encode()).hexdigest()[:32]


# This is a document-revision cache, NOT a time cache: sync, corrections,
# metadata edits, resolutions and deletion all change its key. Ignore usage
# counters, which cannot change a question. Four snapshots bound retained text.
_CONFLICT_FIELDS = ("id", "origin", "revision", "text", "when", "always", "key", "scope", "worker",
                    "protected", "provenance", "conflicted", "retired_at", "retired_reason",
                    "superseded_by", "supersession_confirmed", "updated_at")


def protected_conflicts(entries: list[dict], *, skip: set[str] | frozenset[str] = frozenset(),
                        limit: int = MAX_OPEN_CONFLICTS,
                        disjoint: bool = True) -> list[tuple[dict, dict, str]]:
    """Bounded questions for this exact snapshot; return the caller's rows."""
    refs = _cached_conflict_refs(_conflict_revision(entries), frozenset(skip), limit, disjoint)
    return [(entries[a], entries[b], reason) for a, b, reason in refs]


def _conflict_revision(entries: list[dict]) -> tuple:
    # Raw legacy rows and MemoryEntry rows must have identical scope semantics.
    return tuple(tuple((e.get(f) or "global") if f == "scope" else e.get(f)
                       for f in _CONFLICT_FIELDS) for e in entries)


def coexistence_answer_states(entries: list[dict]) -> dict[str, bool]:
    """Current exact-pair answers vs stale historical records (Recall only)."""
    return dict(_cached_answer_matches(_conflict_revision(entries))[0])


@lru_cache(maxsize=4)
def _cached_answer_matches(revision: tuple) -> tuple:
    from fluid.codemode.facts import global_id, is_superseded
    from fluid.context.screening import screen_findings
    rows = [dict(zip(_CONFLICT_FIELDS, values)) for values in revision]
    active = [e for e in rows if e.get("protected") and not is_superseded(e)
              and not e.get("retired_at") and e.get("provenance") != "tool" and e.get("text")]
    answers = {e["key"] for e in active if str(e.get("key") or "").startswith(COEXIST_KEY_PREFIX)
               and not screen_findings(e["text"]) and not screen_findings(e.get("when") or "")}
    if not answers:
        return (), frozenset()
    # Replica-conflicted wordings of the same exact answer are both closure.
    rules = [e for e in active if not e.get("conflicted")
             and not str(e.get("key") or "").startswith(COEXIST_KEY_PREFIX)]
    tokens = {_coexistence_token(e): e for e in rules}
    states, pairs, legacy = {}, set(), set()
    for key in answers:
        parts = key[len(COEXIST_KEY_PREFIX):].split(".")
        states[key] = False
        if len(parts) == 3 and parts[0] == "v2":
            a, b = (tokens.get(token) for token in parts[1:])
            if a is not None and b is not None and a is not b:
                states[key] = True
                pairs.add(frozenset((global_id(a), global_id(b))))
        else:
            legacy.add(key)
    # Compatibility with already issued opaque keys; serialize each endpoint
    # once, not once for every candidate. No legacy record is rewritten/deleted.
    if legacy:
        values = sorted((_coexistence_value(e), global_id(e)) for e in rules)
        encoded = [(json.dumps(value, ensure_ascii=False), gid) for value, gid in values]
        for i, (first, a) in enumerate(encoded):
            for second, b in encoded[i + 1:]:
                key = COEXIST_KEY_PREFIX + hashlib.sha256(("[" + first + ", " + second + "]").encode()).hexdigest()[:32]
                if key in legacy:
                    states[key] = True
                    pairs.add(frozenset((a, b)))
                    legacy.remove(key)
                    if not legacy:
                        break
            if not legacy:
                break
    return tuple(states.items()), frozenset(pairs)


@lru_cache(maxsize=4)
def _cached_conflict_refs(revision: tuple, skip: frozenset, limit: int, disjoint: bool) -> tuple:
    rows = [dict(zip(_CONFLICT_FIELDS, values)) for values in revision]
    indices = {id(e): i for i, e in enumerate(rows)}
    pairs = _find_protected_conflicts(rows, skip=skip, limit=limit, disjoint=disjoint)
    return tuple((indices[id(a)], indices[id(b)], why) for a, b, why in pairs)


def _find_protected_conflicts(entries: list[dict], *, skip: frozenset[str],
                             limit: int, disjoint: bool) -> list[tuple[dict, dict, str]]:
    """Same-topic pairs of live user-kept facts where the newer one changes
    the rule: ``[(old, new, reason)]``, strongest first, at most ``limit``.

    Model-free and conservative. Both facts are user-kept, live, unlinked,
    in the same scope and on the same worker, and neither is marked as an
    exception or a guess (supersession's own screens). Then the two carry
    the same key and say different things, or route a job both
    name to different models (:func:`model_routing`), or are on one
    :data:`PREFERENCE_TOPICS` topic, name different value words for it and
    share at least two other subject words. Each fact appears in at most one
    pair, and each newer fact is paired only with the older fact closest to
    it, so a cluster of three versions of one rule asks one question.
    ``skip`` holds pair keys already proposed once.
    """
    from fluid.codemode.facts import fingerprint, global_id, is_superseded
    from fluid.codemode.supersession import conflict_scope_evidence

    from fluid.context.screening import screen_findings

    owner = [e for e in entries
             if e.get("protected") and not is_superseded(e) and not e.get("conflicted")
             and not e.get("retired_at") and e.get("provenance") != "tool" and e.get("text")]
    resolved_pairs = _cached_answer_matches(_conflict_revision(entries))[1]
    live = [e for e in owner
            if not str(e.get("key") or "").startswith((*_BIOGRAPHY_KEYS, COEXIST_KEY_PREFIX))]
    topics = {e["id"]: preference_topics(e["text"]) for e in live}
    routes = {e["id"]: model_routing(e["text"]) for e in live}
    salient = {e["id"]: _salient(e["text"]) for e in live}
    fingerprints = {e["id"]: fingerprint(e["text"]) for e in live}
    # These regex screens depend on each text, not on its O(n²) pairings.
    applicable = {e["id"]: conflict_scope_evidence(e, e) for e in live}
    # Most documents have no differing values at all. Index possible changes
    # once, rather than compare every pair of identical-topic/no-topic facts.
    from collections import Counter, defaultdict
    keys = Counter(e.get("key") for e in live if e.get("key"))
    route_values, topic_values = defaultdict(set), defaultdict(set)
    for entry in live:
        for job, models in routes[entry["id"]].items():
            route_values[job].add(models)
        for topic, values in topics[entry["id"]].items():
            topic_values[topic].add(values)
    changed_jobs = {job for job, values in route_values.items() if len(values) > 1}
    changed_topics = {topic for topic, values in topic_values.items() if len(values) > 1}
    edges = {global_id(e) for e in live if e.get("superseded_by")}
    edges.update(e["superseded_by"] for e in live if e.get("superseded_by"))
    live = [e for e in live if (keys.get(e.get("key"), 0) > 1 or global_id(e) in edges
            or changed_jobs.intersection(routes[e["id"]])
            or changed_topics.intersection(topics[e["id"]]))]
    candidates: list[tuple[int, int, float, dict, dict, str]] = []
    for new in live:
        for old in live:
            if old is new or float(old.get("updated_at") or 0) >= float(new.get("updated_at") or 0):
                continue
            if (old.get("scope", "global") != new.get("scope", "global")
                    or old.get("worker") != new.get("worker")):
                continue
            if fingerprints[old["id"]] == fingerprints[new["id"]]:
                continue  # a restatement, the duplicate check's business
            same_key = bool(old.get("key")) and old.get("key") == new.get("key")
            reason = ""
            strength = 0
            # A question never hides the wider rule: any shared job with
            # different typed routing is worth asking about, not superseding.
            old_routes, new_routes = routes[old["id"]], routes[new["id"]]
            moved = sorted(job for job in old_routes.keys() & new_routes.keys()
                           if old_routes[job] != new_routes[job])
            if old.get("superseded_by") == global_id(new) and not old.get("supersession_confirmed"):
                reason, strength = "unconfirmed replacement proposal", 3
            elif same_key:
                reason, strength = f"same key {old['key']}", 2
            elif moved:
                reason, strength = "model routing changed for " + ", ".join(moved), 1
            else:
                for topic in topics[old["id"]].keys() & topics[new["id"]].keys():
                    values_old, values_new = topics[old["id"]][topic], topics[new["id"]][topic]
                    if values_old == values_new:
                        continue  # the same rule worded differently
                    spec = PREFERENCE_TOPICS[topic]
                    shared = (salient[old["id"]] & salient[new["id"]]) - spec["value_words"]
                    if len(shared) >= 2:
                        reason, strength = f"same topic {topic}", 1
                        break
            if not strength:
                continue
            # Typed disagreement is enough to ASK, never enough to link.
            # Keep the scope/temporary/uncertain screens, not the stricter
            # replacement wording needed for automatic supersession.
            if not applicable[old["id"]] or not applicable[new["id"]]:
                continue
            if resolved_pairs and frozenset((global_id(old), global_id(new))) in resolved_pairs:
                continue
            overlap = len(salient[old["id"]] & salient[new["id"]])
            candidates.append((strength, overlap, float(new.get("updated_at") or 0), old, new, reason))
    candidates.sort(key=lambda c: (-c[0], -c[2], -c[1]))
    used: set[str] = set()
    out: list[tuple[dict, dict, str]] = []
    for _strength, _overlap, _at, old, new, reason in candidates:
        if len(out) >= limit:
            break
        if ((disjoint and (old["id"] in used or new["id"] in used))
                or _pair_key(old, new) in skip):
            continue
        used.update((old["id"], new["id"]))
        out.append((old, new, reason))
    return out


def owner_conflict_lines(entries: list[dict], *, questions: list | None = None) -> list[str]:
    """Existing footer; answers use Remember, not replacement or local state."""
    from fluid.codemode.facts import global_id
    return [
        f"- [owner question; unresolved conflict] Saved facts [{global_id(old)}] and "
        f"[{global_id(new)}] disagree ({reason}). Both facts remain usable as context "
        "and model-routing authority. Recall both and ask the owner which rule applies; "
        "do not silently choose a model for the contested job. If the owner says both stand, "
        f"Remember their answer with key={coexistence_key(old, new)!r}, user_requested=true, "
        f"scope={'global' if old.get('scope') in (None, 'global') else 'project'!r}, "
        'when="When applying these two owner rules"; no replaces. '
        "This closes this pair on synced workers until either fact changes."
        for old, new, reason in (protected_conflicts(entries) if questions is None else questions)
    ]


def open_conflicts(entries: list[dict]) -> int:
    """Bounded non-link questions; correction, replacement or keep-both closes one."""
    return len(protected_conflicts(entries))


def _load_curation(root: Path) -> dict:
    try:
        doc = json.loads((root / CONFLICT_CURATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def curate_protected_conflicts(store: Any, *, now: float | None = None) -> list[str]:
    """Record which bounded owner questions were surfaced. Never link facts.

    The existing question footer derives open pairs from the same live
    snapshot. A confirmed replacement, correction, Forget or replicated
    keep-both answer closes a pair; another candidate can then use its slot.
    No owner text is hidden.
    """
    from fluid.codemode.facts import visible_on
    from fluid.codemode.skills import atomic_write_text

    now = time.time() if now is None else now
    try:
        doc = store._facts.snapshot()
        entries = [e for e in doc["entries"] if visible_on(e, doc.get("store_id"))]
        root = Path(store.root)
        curation = _load_curation(root)
        proposed = [p for p in curation.get("proposed") or [] if isinstance(p, str)]
        changed: list[str] = []
        # Select the SAME capped questions the footer shows before skipping
        # previously surfaced pairs; a second pass must not open another cap.
        for old, new, _reason in protected_conflicts(entries):
            pair = _pair_key(old, new)
            if pair not in proposed:
                proposed.append(pair)
                changed.append(f"conflict-memory:{old['id']}")
        if proposed != list(curation.get("proposed") or []):
            curation["proposed"] = proposed[-MAX_REMEMBERED_PAIRS:]
            curation["updated_at"] = now
            atomic_write_text(root / CONFLICT_CURATION_FILE, json.dumps(curation))
        return changed
    except Exception:  # noqa: BLE001 — curation is opportunistic, never fatal
        return []

"""Standing-preference proposals and conflict curation for user-kept facts.

The background learner may not write a fact the owner asked to keep
(``protected``), yet those are the facts that go stale when the owner changes
how they want the work done. This module gives the learner two ways to
raise the question, both bounded so they cannot nag:

* :class:`PreferenceProposals`: when a synthesis pass sees the user correct
  HOW the agent works (model choice, delegation, reviews, CI, deploys,
  process) it proposes the whole current rule for a ``pref.<topic>`` fact.
  The proposal is queued in ``pref-proposals.jsonl`` beside the store, never
  saved as a fact, and the next sessions' ``# Memory`` snapshot shows it as
  one line asking the agent to confirm it with the owner. At most
  :data:`MAX_OPEN_PREFERENCE_PROPOSALS` are open, one per topic (a newer
  wording replaces the older), and each expires after
  :data:`PREFERENCE_PROPOSAL_TTL`. A proposal closes itself once a user-kept
  fact keyed ``pref.<topic>`` is saved after it. The guidance queue
  (``pending-proposals.jsonl``) is not reused: ``fluid evolve`` replays
  everything in it through the guidance gate.

* :func:`curate_protected_conflicts`: deterministic, model-free detection of
  two live user-kept facts about the same how-the-agent-works topic where
  the newer one changes the rule. The pair is linked through the existing
  supersession proposal, which keeps both texts and renders the snapshot's
  conflict line ("Recall the pair and ask the owner to resolve it"). It never
  archives or rewrites a user-kept fact. At most
  :data:`MAX_OPEN_CONFLICTS` such questions are open at once, and a pair is
  proposed once: after the owner resolves it, it is not raised again.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

PREFERENCE_PROPOSALS_FILE = "pref-proposals.jsonl"
CONFLICT_CURATION_FILE = "conflict-curation.json"
MAX_OPEN_PREFERENCE_PROPOSALS = 3
PREFERENCE_PROPOSAL_TTL = 14 * 86400.0
PREFERENCE_KEY_PREFIX = "pref."
MAX_OPEN_CONFLICTS = 3
# Pairs already proposed, remembered so a resolved question is not asked
# again (newest kept).
MAX_REMEMBERED_PAIRS = 128

_TOPIC_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_topic(raw: Any) -> str | None:
    """``subagent_models`` from ``subagent_models``, ``pref.subagent_models``
    or ``Subagent-Models``; None when it is not a short snake_case name."""
    topic = str(raw or "").strip().casefold()
    if topic.startswith(PREFERENCE_KEY_PREFIX):
        topic = topic[len(PREFERENCE_KEY_PREFIX):]
    topic = re.sub(r"[\s\-.]+", "_", topic)
    return topic if _TOPIC_RE.fullmatch(topic) else None


def _fingerprint(text: str) -> str:
    from fluid.codemode.facts import fingerprint
    return fingerprint(text)


def _scope_visible(scope: str, project: str | None) -> bool:
    return project is None or scope in ("global", project)


class PreferenceProposals:
    """The bounded queue of proposed ``pref.<topic>`` updates for one store."""

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

    def _saved_preferences(self, entries: list[Any] | None) -> list[tuple[str, str, float, str]]:
        """``(key, scope, updated_at, fingerprint)`` of live user-kept
        ``pref.*`` facts (dicts or MemoryEntry objects)."""
        if entries is None:
            try:
                entries = self._store.memories()
            except Exception:  # noqa: BLE001 — unreadable store: nothing resolves
                entries = []
        out = []
        for entry in entries:
            get = entry.get if isinstance(entry, dict) else (lambda k, e=entry: getattr(e, k, None))
            key = str(get("key") or "")
            if get("protected") and key.startswith(PREFERENCE_KEY_PREFIX):
                out.append((key, str(get("scope") or "global"),
                            float(get("updated_at") or get("created_at") or 0.0),
                            _fingerprint(str(get("text") or ""))))
        return out

    def _is_open(self, record: dict, saved, now: float) -> bool:
        if now - float(record["ts"]) >= PREFERENCE_PROPOSAL_TTL:
            return False
        key = PREFERENCE_KEY_PREFIX + normalize_topic(record["topic"])
        scope = record.get("scope") or "global"
        # Answered: the owner's confirmation (or correction) was saved under
        # the topic's key after the proposal was made.
        return not any(k == key and (s == scope or s == "global") and at >= float(record["ts"])
                       for k, s, at, _ in saved)

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
        """Queue a proposed rule for ``pref.<topic>``.

        Returns ``proposed`` (queued, or replaced the topic's older wording),
        ``duplicate`` (the same wording is already open), ``saved`` (a
        user-kept fact under that key already says it) or ``full`` (the
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
        key = PREFERENCE_KEY_PREFIX + name
        fingerprint = _fingerprint(body)
        with journal_lock(self._lock, wait=1.0) as held:
            if not held:
                raise ValueError("preference proposals are busy; the next pass retries")
            saved = self._saved_preferences(None)
            if any(k == key and s in (scope, "global") and fp == fingerprint for k, s, _, fp in saved):
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
        key = PREFERENCE_KEY_PREFIX + normalize_topic(record["topic"])
        lines.append(
            f"- [{key}; proposed, not saved] {record['text']} (The learner read this in the "
            f"owner's recent corrections. Confirm it with the owner, then save it with Remember "
            f"key={key}; if the owner's current message already says it, save it without asking.)"
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
_CLAUSE_RE = re.compile(r"[;\n]|\.(?:\s|$)")


def model_routing(text: str) -> dict[str, frozenset[str]]:
    """``{job: models}`` a fact assigns, from clauses that name both."""
    routes: dict[str, frozenset[str]] = {}
    for clause in _CLAUSE_RE.split((text or "").casefold()):
        # "code review" is a review, not code work.
        words = _words(re.sub(r"\bcode[\s-]+review", "review", clause))
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
        "value_words": frozenset("github unraid gamingpc local locally machines paid larger nightly".split()),
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


def protected_conflicts(entries: list[dict], *, skip: set[str] | frozenset[str] = frozenset(),
                        limit: int = MAX_OPEN_CONFLICTS) -> list[tuple[dict, dict, str]]:
    """Same-topic pairs of live user-kept facts where the newer one changes
    the rule: ``[(old, new, reason)]``, strongest first, at most ``limit``.

    Model-free and conservative. Both facts are user-kept, live, unlinked,
    in the same scope and on the same worker, and neither is marked as an
    exception or a guess (supersession's own screens). Then the two carry
    the same ``pref.*`` key and say different things, or route a job both
    name to different models (:func:`model_routing`), or are on one
    :data:`PREFERENCE_TOPICS` topic, name different value words for it and
    share at least two other subject words. Each fact appears in at most one
    pair, and each newer fact is paired only with the older fact closest to
    it, so a cluster of three versions of one rule asks one question.
    ``skip`` holds pair keys already proposed once.
    """
    from fluid.codemode.facts import fingerprint
    from fluid.codemode.supersession import replacement_evidence

    live = [e for e in entries
            if e.get("protected") and not e.get("superseded_by") and not e.get("conflicted")
            and not e.get("retired_at") and e.get("provenance") != "tool" and e.get("text")
            and not str(e.get("key") or "").startswith(_BIOGRAPHY_KEYS)]
    topics = {e["id"]: preference_topics(e["text"]) for e in live}
    routes = {e["id"]: model_routing(e["text"]) for e in live}
    salient = {e["id"]: _salient(e["text"]) for e in live}
    candidates: list[tuple[int, int, float, dict, dict, str]] = []
    for new in live:
        for old in live:
            if old is new or float(old.get("updated_at") or 0) >= float(new.get("updated_at") or 0):
                continue
            if (old.get("scope", "global") != new.get("scope", "global")
                    or old.get("worker") != new.get("worker")):
                continue
            if fingerprint(old["text"]) == fingerprint(new["text"]):
                continue  # a restatement, the duplicate check's business
            same_key = (str(old.get("key") or "").startswith(PREFERENCE_KEY_PREFIX)
                        and old.get("key") == new.get("key"))
            reason = ""
            strength = 0
            # The newer routing must restate every job the older one routes:
            # the conflict line hides the older fact, and a newer fact about
            # reviews alone must not hide where code work goes.
            old_routes, new_routes = routes[old["id"]], routes[new["id"]]
            moved = sorted(job for job in old_routes if old_routes[job] != new_routes.get(job)) \
                if old_routes and old_routes.keys() <= new_routes.keys() else []
            if same_key:
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
            # The supersession screens: newer, same subject, no exception or
            # hedge in either text, scopes that may replace each other.
            if not replacement_evidence(old, new, proposed=True):
                continue
            overlap = len(salient[old["id"]] & salient[new["id"]])
            candidates.append((strength, overlap, float(new.get("updated_at") or 0), old, new, reason))
    candidates.sort(key=lambda c: (-c[0], -c[2], -c[1]))
    used: set[str] = set()
    out: list[tuple[dict, dict, str]] = []
    for _strength, _overlap, _at, old, new, reason in candidates:
        if len(out) >= limit:
            break
        if old["id"] in used or new["id"] in used or _pair_key(old, new) in skip:
            continue
        used.update((old["id"], new["id"]))
        out.append((old, new, reason))
    return out


def open_conflicts(entries: list[dict]) -> int:
    """User-kept facts waiting for the owner to resolve a replacement."""
    return sum(1 for e in entries
               if e.get("protected") and e.get("superseded_by") and not e.get("supersession_confirmed"))


def _load_curation(root: Path) -> dict:
    try:
        doc = json.loads((root / CONFLICT_CURATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def curate_protected_conflicts(store: Any, *, now: float | None = None) -> list[str]:
    """Propose the open conflicts :func:`protected_conflicts` finds, up to
    :data:`MAX_OPEN_CONFLICTS` open at once; returns ``conflict-memory:<old>``
    for each pair linked. Never raises; never archives or rewrites a fact."""
    from fluid.codemode.facts import visible_on
    from fluid.codemode.skills import atomic_write_text

    now = time.time() if now is None else now
    try:
        doc = store._facts.snapshot()
        entries = [e for e in doc["entries"] if visible_on(e, doc.get("store_id"))]
        room = MAX_OPEN_CONFLICTS - open_conflicts(entries)
        if room <= 0:
            return []
        root = Path(store.root)
        curation = _load_curation(root)
        proposed = [p for p in curation.get("proposed") or [] if isinstance(p, str)]
        changed: list[str] = []
        for old, new, _reason in protected_conflicts(entries, skip=set(proposed), limit=room):
            try:
                linked = store.propose_memory_supersession(
                    old["id"], new["id"], old_revision=old.get("revision", 1),
                    new_revision=new.get("revision", 1), protected_conflict=True,
                )
            except ValueError:
                continue  # changed since it was read; the next pass looks again
            proposed.append(_pair_key(old, new))
            if linked:
                changed.append(f"conflict-memory:{old['id']}")
        if proposed != list(curation.get("proposed") or []):
            curation["proposed"] = proposed[-MAX_REMEMBERED_PAIRS:]
            curation["updated_at"] = now
            atomic_write_text(root / CONFLICT_CURATION_FILE, json.dumps(curation))
        return changed
    except Exception:  # noqa: BLE001 — curation is opportunistic, never fatal
        return []

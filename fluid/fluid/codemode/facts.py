"""Bounded fact storage. One locked atomic document is the commit boundary.

Live facts, archive, revisions, deletion metadata and the replication snapshot
are committed together. Skills and inference journals are separate resources.
No model is needed to enforce these rules. The app supplies authenticated owner
and project identities; standalone Fluid uses an installation-local owner.
"""
from __future__ import annotations

from dataclasses import fields
import hashlib
import json
import logging
import math
import re
import time
import uuid
from pathlib import Path

from fluid.codemode.transactions import file_transaction

_log = logging.getLogger(__name__)

# Three tiers:
#
# * explicit (``protected``): the user asked for it or corrected a fact. No
#   TTL, never touched by the learner, never removed or archived by any
#   automatic path. At MAX_EXPLICIT user-saved facts (live and archived
#   together) a new one is refused with a reason the agent can relay;
#   nothing already saved is dropped to make room.
# * durable (``durable``): an agent decided a fact was worth keeping
#   (Remember without ``user_requested``). Longer TTL than inferred, renewed
#   when recalled or re-saved, curated by consolidation, and archived past
#   MAX_DURABLE. The store filled its 256-slot explicit quota in three weeks
#   when every agent save was explicit, then refused every new save.
# * inferred: learner output and tool harvests, as before.
#
# Durable and inferred facts never refuse a save for lack of room.
MAX_EXPLICIT = 512
MAX_DURABLE = 120
EXPLICIT_FULL_MESSAGE = (
    "Memory is full: {cap} facts are saved at the user's request. Forget or merge "
    "one first; nothing was saved.")
DURABLE_TTL = 120 * 86400
# An unprotected fact moves to the archive when it expires and is removed
# for good this long after its expiry date, so an expiry can be undone by
# saving the fact again for a year. Recall finds it until then.
ARCHIVE_RETENTION = 365 * 86400
# Recalls count as this much recent activity each (capped), so a fact that
# keeps being looked up outranks one that was merely written recently.
USE_CREDIT = 7 * 86400
MAX_USE_CREDIT = 10
# A legacy fact demoted from explicit never expires sooner than this.
MIGRATION_GRACE = 30 * 86400
# A non-explicit fact about one branch or named worktree ("fix/x has its own
# .venv") stops being true when the branch merges. A fifth of one store's
# explicit quota was such receipts. They live this long unless recalled.
VOLATILE_TTL = 14 * 86400
VOLATILE_GRACE = 7 * 86400
_BRANCH_REF = re.compile(
    r"(?<![\w/.-])(?:feat|fix|chore|refactor|perf|hotfix|release)/[a-z0-9][a-z0-9-]*(?![\w/.])"
    r"|\b(?:worktree|checkout)\s+[a-z0-9]+(?:-[a-z0-9]+)+\b"
    r"|\b[a-z0-9]+(?:-[a-z0-9]+)+\s+(?:(?:Linux|Windows)\s+)?(?:worktree|checkout)\b"
    r"|\.fluid/worktrees/[\w.-]+")
MAX_TOMBSTONES = 256
BLOOM_BYTES = 16 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
# The # Memory snapshot (see SkillStore.memory_snapshot_plan) is one style of
# memory: every fact has a WHEN line (the situation that should make a future
# agent load it) and its full text. The snapshot lists the when-lines of the
# facts in scope as an index, in this many characters; the model Recalls a
# fact by id for its full text. Sized so a ~200-fact project store shows most
# of its index (a when-line plus its id label is ~120-180 characters).
MAX_PROMPT_CHARS = 10_000
# A memory's when-line: 1 to MAX_WHEN_CHARS characters. A fact saved without
# one (older stores, the learner, harvests) is listed by a line derived from
# its text at render time (:func:`derived_when`), at most DERIVED_WHEN_CHARS;
# the derivation is never stored as if it had been written.
MAX_WHEN_CHARS = 140
DERIVED_WHEN_CHARS = 100
# ALWAYS memories (saved at the owner's request with always=true) render in
# full every time, above the index. Those visible in one project (global plus
# that project's) share this many characters of text; a save that would
# exceed it is refused with the list of current ones, so the agent asks the
# owner which to drop. The snapshot enforces the same bound on render.
ALWAYS_CHARS = 3000
INFERRED_TTL = 90 * 86400
MAX_INFERENCE_CALLS_PER_DAY = 48
HISTORY_TTL = 30 * 86400
MAX_HISTORY = 2
SCHEMA = 2
# Remember(replaces=[...]) names at most this many superseded facts per save.
MAX_REPLACES = 16
# Write-time overlap hint: a new save lists up to MAX_SIMILAR live in-scope
# facts sharing at least SIMILAR_MIN_SHARED subject terms, which must also be
# SIMILAR_MIN_RATIO of the smaller fact's terms. Tuned on a 261-fact owner
# store: it links the subagent-routing rules to each other while unrelated
# pairs (sampled at random) cross it under 1% of the time.
MAX_SIMILAR = 3
SIMILAR_MIN_SHARED = 5
SIMILAR_MIN_RATIO = 0.25
_ID_REF = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
# Words that join unrelated rules ("never", "one per") without naming a subject.
_HINT_NOISE = frozenset(
    "not never non one per own set all any only also each every always keep stay stays "
    "must still same without".split())


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'`])")


def derived_when(text):
    """A when-line for a fact saved without one: its first sentence, or
    that sentence's first clause when the sentence is too long, at most
    DERIVED_WHEN_CHARS. An ellipsis marks text left out. Render-time only,
    never stored."""
    text = " ".join(str(text or "").split())
    first = _SENTENCE_END.split(text, 1)[0]
    if len(first) > DERIVED_WHEN_CHARS:
        first = re.split(r";\s+", first, maxsplit=1)[0].rstrip(" ,:")
    if len(first) <= DERIVED_WHEN_CHARS:
        return first + (" …" if len(first) < len(text) else "")
    cut = first[:DERIVED_WHEN_CHARS - 1]
    cut = cut.rsplit(" ", 1)[0] if " " in cut[DERIVED_WHEN_CHARS // 2:] else cut
    return cut.rstrip(" ,;:-") + "…"


def when_line(entry):
    """The fact's written when-line, or one derived from its text."""
    get = entry.get if isinstance(entry, dict) else (lambda k: getattr(entry, k, None))
    return get("when") or derived_when(get("text"))


def validate_when(when):
    """A written when-line, whitespace-normalized; raises ValueError."""
    from fluid.context.screening import looks_like_secret, screen_findings
    if not isinstance(when, str):
        raise ValueError("when must be a string")
    when = " ".join(when.split())
    if not when or len(when) > MAX_WHEN_CHARS:
        raise ValueError(f"when must contain 1–{MAX_WHEN_CHARS} characters: the situation "
                         "in which a future agent should load this memory")
    if looks_like_secret(when):
        raise ValueError("when contains a credential")
    if screen_findings(when):
        raise ValueError("when matches an injection pattern")
    return when


def always_applies(entry):
    """An ALWAYS memory: owner-saved, flagged always, live and current."""
    get = entry.get if isinstance(entry, dict) else (lambda k: getattr(entry, k, None))
    return bool(get("always") and get("protected") and not get("superseded_by"))


def fingerprint(text):
    return hashlib.sha256(" ".join(text.casefold().split()).encode()).hexdigest()


def canonical_key(text):
    # Deliberately narrow. Do not equate nicknames, legal names, or someone
    # else's name. Callers can supply a slot for other explicit corrections.
    if re.match(r"^(?:the )?user(?:'s|’s) (?:preferred )?name is\b", text, re.I):
        return "profile.name"
    return None


def usefulness(entry):
    """Eviction and display rank: last activity plus credit for recalls."""
    activity = max(float(entry.get("updated_at") or 0), float(entry.get("last_recalled_at") or 0))
    return activity + min(int(entry.get("uses") or 0), MAX_USE_CREDIT) * USE_CREDIT


def tier(entry):
    if entry.get("protected"):
        return "explicit"
    return "durable" if entry.get("durable") else "inferred"


def volatile(text):
    """Whether a fact names one branch or worktree and so goes stale fast."""
    return bool(_BRANCH_REF.search(text or ""))


def lifetime(entry):
    """Seconds a non-explicit fact lives past its last save or recall."""
    if volatile(entry.get("text")):
        return VOLATILE_TTL
    return DURABLE_TTL if entry.get("durable") else INFERRED_TTL


# Legacy classification. Before tiers existed every Remember call was
# explicit, so an old explicit fact is kept explicit only when it reads as
# the user's own statement, preference or decision; the rest were facts an
# agent chose to save and become durable. Deterministic, so every worker
# classifies a replicated fact the same way without a clock bump.
_USER_KEY = re.compile(r"^(?:profile|preferences?|career|business|user|owner)[.:_-]", re.IGNORECASE)
_USER_SUBJECT = re.compile(r"^(?:the\s+)?(?:user|owner)(?:['’]s)?\b", re.IGNORECASE)
_USER_INTENT = re.compile(
    r"\b(?:user|owner)\s+(?:has\s+|had\s+)?(?:explicitly\s+|now\s+|later\s+)?"
    r"(?:wants?|wanted|requires?|required|expects?|chose|approved|prefers?|preferred|"
    r"corrected|confirmed|deferred|asked|requested|decided|clarified|reports?|reported|"
    r"owns|said|says|means|likes?|dislikes?|insists?|instructed)\b", re.IGNORECASE)


def legacy_saved_by(entry):
    if entry.get("provenance") == "user" or _USER_KEY.match(entry.get("key") or ""):
        return "user"
    text = entry.get("text") or ""
    return "user" if _USER_SUBJECT.match(text) or _USER_INTENT.search(text) else "agent"


def validate_text(text):
    from fluid.codemode.skills import MAX_MEMORY_CHARS
    from fluid.context.screening import looks_like_secret
    text = " ".join(str(text or "").split())
    if not text or len(text) > MAX_MEMORY_CHARS:
        raise ValueError(f"Memory must contain 1–{MAX_MEMORY_CHARS} characters")
    if looks_like_secret(text):
        raise ValueError("Memory contains a credential; save its location, not its value")
    return text


def barrier_contains(barrier, value):
    bits = bytes.fromhex(barrier) if barrier else bytes(BLOOM_BYTES)
    if len(bits) != BLOOM_BYTES:
        raise ValueError("Invalid deletion barrier")
    digest = hashlib.sha256(value.encode()).digest()
    return all(bits[(bit := int.from_bytes(digest[i:i + 4], "big") % (BLOOM_BYTES * 8)) // 8] & (1 << (bit % 8)) for i in range(0, 16, 4))


def barrier_add(barrier, value):
    bits = bytearray.fromhex(barrier) if barrier else bytearray(BLOOM_BYTES)
    digest = hashlib.sha256(value.encode()).digest()
    for i in range(0, 16, 4):
        bit = int.from_bytes(digest[i:i + 4], "big") % (BLOOM_BYTES * 8)
        bits[bit // 8] |= 1 << (bit % 8)
    return bits.hex()


def global_id(entry):
    return f"{entry['origin']}:{str(entry['id']).split(':')[-1]}"


def visible_on(entry, store_id):
    """Whether a fact applies on the worker whose store is ``store_id``.

    Environment facts harvested from tool output (interpreter and checkout
    paths, where the browser runs) carry the ``worker`` (store ID) they
    describe. They replicate like any fact but are shown, recalled and
    curated only on that worker, so two workers no longer overwrite one
    shared copy with their own paths. Every other fact has no ``worker``
    and is shared.
    """
    return not entry.get("worker") or entry.get("worker") == store_id


class FactStore:
    def __init__(self, root: Path, owner="local"):
        self.root, self.owner = Path(root), owner
        self.path = self.root / "memories.json"
        self.lock = self.root / ".memory.lock"

    def _load(self):
        from fluid.codemode.skills import load_json_document
        doc = load_json_document(self.path, {"next_id": 1, "entries": []})
        if doc.get("schema", 1) > SCHEMA:
            raise ValueError("Memory was written by a newer application; refusing to overwrite it")
        if doc.get("owner", self.owner) != self.owner:
            raise ValueError("Memory owner mismatch")
        doc.setdefault("next_id", 1)
        doc.setdefault("store_id", uuid.uuid4().hex)
        for key in ("entries", "archived", "tombstones"):
            if not isinstance(doc.get(key), list):
                doc[key] = []
        return doc

    def _write(self, doc):
        from fluid.codemode.skills import atomic_write_text
        doc.update(schema=SCHEMA, owner=self.owner)
        payload = json.dumps(doc, ensure_ascii=False, indent=2)
        if len(payload.encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("Memory capacity reached; no changes saved")
        atomic_write_text(self.path, payload)

    def _prepare(self):
        doc = self._load()
        changed = False
        legacy = self.root / ".archive" / "memories-archive.jsonl"
        if doc.get("schema", 1) < SCHEMA:
            seen = {e.get("id") for e in doc["entries"] if isinstance(e, dict)}
            if legacy.exists():
                for line in legacy.read_text(encoding="utf-8").splitlines():
                    try:
                        e = json.loads(line)
                        if isinstance(e, dict) and e.get("id") and e.get("id") not in seen:
                            doc["archived"].append(e)
                            seen.add(e["id"])
                    except ValueError:
                        continue
            for e in doc["entries"] + doc["archived"]:
                if isinstance(e, dict):
                    e.setdefault("revision", 1)
                    e.setdefault("protected", False)
                    e.setdefault("provenance", "session")
                    e.setdefault("expires_at", time.time() + INFERRED_TTL)
                    e.setdefault("origin", e["id"].rsplit(":", 1)[0] if ":" in e["id"] else doc["store_id"])
                    e.setdefault("clock", {e["origin"]: e["revision"]})
            changed = bool(doc["entries"] or doc["archived"])
        changed = self._maintain(doc) or changed
        if changed:
            self._write(doc)
        # Removal follows a committed migration, never precedes it. A restart
        # after the write can repeat this cleanup without importing twice.
        if doc.get("schema") == SCHEMA and legacy.exists():
            legacy.unlink()
        return doc

    @staticmethod
    def _classify(e, now):
        """Normalize tier fields; demote legacy agent saves to durable.

        The clock is deliberately left alone: every worker reaches the same
        result for the same record, and an equal clock keeps each side's
        local copy, so classification never replicates as an edit.
        """
        if e.get("protected"):
            e.pop("durable", None)
            if e.get("saved_by") == "user":
                return
            if legacy_saved_by(e) == "user":
                e["saved_by"] = "user"
                return
            activity = max(float(e.get("updated_at") or 0), float(e.get("last_recalled_at") or 0))
            e.update(protected=False, durable=True, saved_by="agent")
            e.pop("always", None)  # only an owner-saved fact applies always
            grace = VOLATILE_GRACE if volatile(e.get("text")) else MIGRATION_GRACE
            e["expires_at"] = max(activity + lifetime(e), now + grace)
        elif "durable" in e and not isinstance(e["durable"], bool):
            e["durable"] = bool(e["durable"])

    def _repair_supersession_links(self, doc):
        """Repair invalid links without resurrecting expired replacement values.

        Keep direct edges: flattening loses learner-proposed intermediate
        replacements, which cannot be recovered from wording after withdrawal.
        Forget/curation restores only the nearest eligible predecessor.
        Expiry, overflow and archive pruning are not evidence of retraction:
        their predecessors keep links and age out on their own. Read paths
        repair legacy links too. Text and lifetime are never renewed here.
        """
        from fluid.codemode.skills import MAX_MEMORIES
        from fluid.codemode.supersession import replacement_evidence
        from fluid.context.screening import screen_findings
        records = doc["entries"] + doc["archived"]
        by_id = {global_id(e): e for e in records}
        forgotten_links = {global_id(e): e.get("superseded_by") for e in doc["tombstones"]}
        forgotten_cache = {}  # one 16-KiB barrier decode per distinct pointer

        def is_forgotten(pointer):
            if pointer not in forgotten_cache:
                forgotten_cache[pointer] = barrier_contains(doc.get("forgotten", ""), "id:" + pointer)
            return forgotten_cache[pointer]

        def deletion_successor(pointer, origin):
            # Deletion bodies form their own graph, including concurrent
            # cross-worker cycles. Resolve it before planning a metadata bump.
            initial, seen = pointer, {origin}
            while pointer:
                if pointer in seen:
                    return None
                seen.add(pointer)
                if not is_forgotten(pointer):
                    return pointer
                if pointer not in forgotten_links:
                    # The barrier outlives compacted bodies. Missing lineage
                    # is not evidence that the newer value was withdrawn.
                    return initial
                pointer = forgotten_links[pointer]
            return None

        now = time.time()
        for _ in range(len(records) + 1):
            plans = []
            restored = []
            for old in records:
                if not old.get("superseded_by"):
                    continue
                node, seen = old, {global_id(old)}
                confirmed = bool(old.get("supersession_confirmed"))
                while pointer := node.get("superseded_by"):
                    if pointer in seen:
                        plans.append((old, None, False))  # void every cycle edge
                        break
                    seen.add(pointer)
                    target = by_id.get(pointer)
                    forgotten = is_forgotten(pointer)
                    retired = (target is not None and target.get("retired_at")
                               and target.get("retired_reason") != "superseded"
                               and not target.get("superseded_by"))
                    if forgotten or retired:
                        if node is old:
                            # A forgotten intermediate may still bridge to a
                            # newer value, including a concurrent replica link.
                            onward = deletion_successor(pointer, global_id(old)) if forgotten else None
                            if onward != old.get("superseded_by"):
                                plans.append((old, onward, confirmed if onward else False))
                        break  # an intermediate retracted leaf is repaired first
                    if target is None:
                        break  # archive pruning is not a retraction
                    old_scope, target_scope = old.get("scope", "global"), target.get("scope", "global")
                    compatible_scope = old_scope == target_scope or (target_scope == "global" and old_scope.startswith("project:"))
                    if (not compatible_scope or old.get("worker") != target.get("worker")
                            or target.get("provenance") == "tool"
                            or (old.get("protected") and not target.get("protected"))
                            or screen_findings(target["text"])):
                        plans.append((old, None, False))
                        break
                    if not target.get("superseded_by"):
                        if (old.get("protected") and target.get("protected")
                                and replacement_evidence(dict(old, superseded_by=None), target)):
                            confirmed = True
                        if (confirmed != bool(old.get("supersession_confirmed"))
                                or (confirmed and old.get("conflicted"))):
                            plans.append((old, old["superseded_by"], confirmed))
                        break
                    confirmed = confirmed and bool(target.get("supersession_confirmed"))
                    node = target
            if not plans:
                break
            for old, pointer, confirmed in plans:
                if pointer:
                    old.update(superseded_by=pointer, supersession_confirmed=confirmed)
                    if confirmed:
                        old.pop("conflicted", None)
                else:
                    old.pop("superseded_by", None)
                    old.pop("supersession_confirmed", None)
                    # A metadata-only race is not a conflict between values.
                    if len(set(old.get("alternatives", []) + [old["text"]])) == 1:
                        old.pop("conflicted", None)
                        old.pop("alternatives", None)
                    category = tier(old)
                    limit = MAX_DURABLE if category == "durable" else MAX_MEMORIES
                    room = old.get("protected") or sum(tier(e) == category for e in doc["entries"]) < limit
                    if (old.get("retired_reason") == "superseded" and room
                            and (old.get("protected") or old.get("expires_at", now + 1) > now)):
                        doc["archived"] = [e for e in doc["archived"] if e is not old]
                        if not any(e is old for e in doc["entries"]):
                            doc["entries"].append(old)
                        old.pop("retired_at", None)
                        old.pop("retired_reason", None)
                    if any(e is old for e in doc["entries"]):
                        restored.append(old)
                self._bump_metadata(doc, old)
            if restored:
                self._apply_supersessions(doc, restored)

    def _maintain(self, doc, keep=None):
        """Expire, classify and bound the store. Never refuses for room.

        ``keep`` is a fact saved in this transaction: it is never the one
        archived to make room for itself.
        """
        from fluid.codemode.skills import MAX_MEMORIES, MAX_ARCHIVED_MEMORIES
        before = json.dumps(doc, sort_keys=True)
        now = time.time()
        for bucket in ("entries", "archived"):
            doc[bucket] = [e for e in doc[bucket] if isinstance(e, dict) and e.get("id") and e.get("text")]
            for e in doc[bucket]:
                e["history"] = [h for h in e.get("history", []) if h.get("at", 0) > now - HISTORY_TTL][-MAX_HISTORY:]
                self._classify(e, now)
        # Expiry does not become a forget tombstone. It travels with the fact
        # so replaying an old snapshot cannot renew its lifetime. An expired
        # live fact moves to the archive, where Recall still finds it and a
        # save brings it back; ARCHIVE_RETENTION after its expiry it is
        # removed for good. User-saved facts never expire.
        # Lineage does not exempt inferred facts from TTL or storage caps.
        self._repair_supersession_links(doc)
        def retained(e):
            return bool(e.get("protected"))
        expired = [e for e in doc["entries"] if not retained(e) and e.get("expires_at", now + 1) <= now]
        if expired:
            gone = {id(e) for e in expired}
            doc["entries"] = [e for e in doc["entries"] if id(e) not in gone]
            doc["archived"] += expired
        ended = [e for e in doc["archived"] if not retained(e)
                 and e.get("expires_at", now + 1) + ARCHIVE_RETENTION <= now]
        if ended:
            gone = {id(e) for e in ended}
            doc["archived"] = [e for e in doc["archived"] if id(e) not in gone]
            self._note_loss(doc, "expired_archived", len(ended), now)
        self._repair_supersession_links(doc)
        live = doc["entries"]
        inferred = [e for e in live if tier(e) == "inferred" and not retained(e)]
        ranked = sorted(inferred, key=lambda e: (e.get("id") == keep, e.get("uses", 0), e.get("updated_at", 0)))
        overflow = ranked[:max(0, len(inferred) - MAX_MEMORIES)]
        durable = [e for e in live if tier(e) == "durable" and not retained(e)]
        ranked = sorted(durable, key=lambda e: (e.get("id") == keep, usefulness(e), str(e.get("id"))))
        overflow += ranked[:max(0, len(durable) - MAX_DURABLE)]
        gone = {id(e) for e in overflow}
        doc["entries"] = [e for e in live if id(e) not in gone]
        archived = doc["archived"] + overflow
        # User-saved facts are never trimmed: a full explicit tier refuses
        # the next save instead (see save). Archived explicit facts (from
        # older versions, which archived past 256) stay until a Forget. The
        # shared archive gives up inferred facts before durable ones, least
        # useful first.
        explicit = [e for e in archived if retained(e)]
        rest = sorted((e for e in archived if not retained(e)),
                      key=lambda e: (tier(e) == "durable", usefulness(e) if e.get("durable") else e.get("updated_at", 0), str(e.get("id"))))
        doc["archived"] = rest[max(0, len(rest) - MAX_ARCHIVED_MEMORIES):] + explicit
        if len(rest) > MAX_ARCHIVED_MEMORIES:
            self._note_loss(doc, "archive_dropped", len(rest) - MAX_ARCHIVED_MEMORIES, now)
        self._repair_supersession_links(doc)
        # Compact deletion bodies; the monotonic barrier survives compaction.
        doc["tombstones"] = doc["tombstones"][-MAX_TOMBSTONES:]
        return before != json.dumps(doc, sort_keys=True)

    @staticmethod
    def _note_loss(doc, kind, count, now):
        """Count facts that left the store without a Forget (local, not
        replicated): totals plus the time of the latest one per kind."""
        losses = doc.get("losses") if isinstance(doc.get("losses"), dict) else {}
        losses[kind] = int(losses.get(kind, 0) or 0) + int(count)
        losses[f"last_{kind}_at"] = now
        doc["losses"] = losses

    def reap(self):
        with file_transaction(self.lock):
            doc = self._load()
            before = len(doc["entries"]) + len(doc["archived"])
            after = self._prepare()
            return max(0, before - len(after["entries"]) - len(after["archived"]))

    def claim_inference_call(self, reserve=0, limit: int | None = None):
        """Claim one of the day's inference calls, leaving ``reserve`` unclaimed.

        ``limit`` is the day's cap (the learner's configured budget); None
        is MAX_INFERENCE_CALLS_PER_DAY."""
        with file_transaction(self.lock):
            doc = self._prepare()
            day = int(time.time() // 86400)
            budget = doc.get("inference_budget", {})
            calls = budget.get("calls", 0) if budget.get("day") == day else 0
            cap = MAX_INFERENCE_CALLS_PER_DAY if limit is None else int(limit)
            share = cap - max(0, reserve)
            if calls >= share:
                if self._note_budget_spent(doc, day):
                    self._write(doc)
                return False
            doc["inference_budget"] = {"day": day, "calls": calls + 1, "last_at": time.time()}
            if calls + 1 >= share:
                self._note_budget_spent(doc, day)
            self._write(doc)
            return True

    @staticmethod
    def _note_budget_spent(doc, day):
        """Record ``day`` as one whose budget (or synthesis share) ran out;
        False when it was already recorded. The last 8 days are kept."""
        days = [d for d in doc.get("inference_budget_spent_days") or [] if isinstance(d, int)]
        if day in days:
            return False
        doc["inference_budget_spent_days"] = sorted(days + [day])[-8:]
        return True

    def inference_budget(self):
        """``{"day", "calls", "last_at"}`` of the day's claims so far, read
        without the lock (the document is replaced atomically); {} when none."""
        budget = self._load().get("inference_budget")
        return dict(budget) if isinstance(budget, dict) else {}

    def inference_budget_spent_days(self):
        """UTC days (``time // 86400``) on which the call budget ran out."""
        days = self._load().get("inference_budget_spent_days")
        return [d for d in days if isinstance(d, int)] if isinstance(days, list) else []

    def snapshot(self):
        with file_transaction(self.lock):
            return self._prepare()

    def entries(self, archived=False):
        """Facts that apply on this worker (see :func:`visible_on`)."""
        doc = self.snapshot()
        return [self._entry(e) for e in doc["archived" if archived else "entries"]
                if visible_on(e, doc["store_id"])]

    def save(self, text, memory_id=None, scope=None, provenance=None, *, protected=False,
             durable=None, key=None, expected_revision=None, source_session=None, restore=False,
             per_worker=False, correction_project=None, replaces=None, receipt=None,
             when=None, always=None, require_when=False):
        """Save one fact in a tier.

        ``protected`` is the explicit tier (the user asked). ``durable=True``
        is an agent's own save. Neither (the learner, harvests) keeps a
        rewritten fact's existing tier and never downgrades one.

        ``per_worker`` marks an environment fact about this worker: it is
        matched by key or text only against this worker's own copy, and
        replaces the shared copy older versions kept under the same key. A
        user-requested save of such a fact makes it shared again.

        ``replaces`` names facts this save supersedes (see :meth:`_supersede`).
        A refused target refuses the whole save, so nothing half-applies.
        ``receipt``, when a dict, is filled with ``replaced`` and ``skipped``
        (IDs and notes) and, for a new protected/durable fact, ``similar``:
        live facts that look like the same subject (:meth:`_similar`).

        ``when`` is the fact's when-line (:func:`validate_when`); a correction
        keeps the existing one unless a new one is given, and
        ``require_when`` (the Remember tool) refuses a new fact without one.
        ``always`` (None keeps the current flag) marks an ALWAYS memory; only
        a protected save may set it, and the in-scope ALWAYS_CHARS budget
        refuses one that would overflow it (:meth:`_check_always_budget`).
        """
        text = validate_text(text)
        if when is not None:
            when = validate_when(when)
        if always is not None and type(always) is not bool:
            raise ValueError("always must be true or false")
        if always and not protected:
            raise ValueError("always is only for a memory the user asked to apply in every "
                             "chat (user_requested=true); nothing was saved")
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,96}", key)):
            raise ValueError("Invalid memory key")
        replaces = list(dict.fromkeys(replaces or ()))
        if replaces:
            if not (protected or durable):
                raise ValueError("Only a Remember save can replace other facts")
            if len(replaces) > MAX_REPLACES:
                raise ValueError(f"replaces names at most {MAX_REPLACES} facts")
            if not all(isinstance(r, str) and _ID_REF.fullmatch(r) for r in replaces):
                raise ValueError("replaces must list memory IDs from Recall")
        receipt = receipt if isinstance(receipt, dict) else {}
        receipt.update(replaced=[], skipped=[], similar=[])
        fp = fingerprint(text)
        with file_transaction(self.lock):
            doc = self._prepare()
            all_entries = doc["entries"] + doc["archived"]
            local = doc["store_id"]
            old = next((e for e in all_entries if e["id"] == memory_id), None) if memory_id else None
            if memory_id and (old is None or not visible_on(old, local)):
                raise ValueError("Unknown memory ID; a rewrite cannot insert a new fact")
            if not memory_id and key and expected_revision is not None:
                # Restrict corrections to the authenticated project's/global
                # slots. A revision of 1 is also sent on genuinely new saves;
                # preserve that contract only if no slot or deletion evidence
                # exists, rather than guessing a correction's target scope.
                scopes = {scope} if scope is not None else {"global", correction_project}
                scopes.discard(None)
                candidates = [e for e in all_entries if e.get("key") == key
                              and e.get("scope", "global") in scopes and visible_on(e, local)]
                # A slot's confirmed predecessors keep their key in the
                # archive; the current value is the one to correct.
                current = [e for e in candidates
                           if not (e.get("superseded_by") and e.get("supersession_confirmed"))]
                candidates = current or candidates
                fresh = (expected_revision == 1
                         and not any(e.get("key") == key
                                     and (correction_project is None or e.get("scope", "global") in scopes)
                                     for e in all_entries)
                         and not any(t.get("key") == key and t.get("scope") in scopes
                                     for t in doc["tombstones"])
                         and not any(barrier_contains(doc.get("forgotten", ""), "key:" + s + ":" + key)
                                     for s in scopes))
                if len(candidates) > 1 or (not candidates and not fresh):
                    raise ValueError("Key correction is ambiguous or unknown in this scope; Recall and supply memory_id or explicit scope")
                if candidates:
                    old = candidates[0]
                    memory_id = old["id"]
            if old is not None and scope is None:
                scope = old.get("scope", "global")
            # Scope-sensitive canonical keys and deletion barriers must use the
            # resolved correction scope, not the new-fact global default.
            key = key or (canonical_key(text) if scope in (None, "global") else None)
            suppressed = barrier_contains(doc.get("forgotten", ""), "text:" + fp)
            if key:
                suppressed = suppressed or barrier_contains(doc.get("forgotten", ""), "key:" + (scope or "global") + ":" + key)
            if suppressed and not (protected and restore):
                raise ValueError("This fact matches a deletion barrier; an explicit restore request is required")
            for t in doc["tombstones"]:
                if fp in t.get("fingerprints", []) or (key and t.get("key") == key and t.get("scope") == (scope or "global")):
                    if not (protected and restore):
                        raise ValueError("This fact was forgotten; an explicit restore request is required")
            if old is None:
                # Another worker's environment fact is never this save's
                # target. A per-worker save matches this worker's copy, or a
                # fact the user saved under that key, which it may not rewrite.
                old = next((e for e in all_entries if e.get("scope", "global") == (scope or "global")
                            and ((e.get("worker") == local or (e.get("protected") and not e.get("worker")))
                                 if per_worker else visible_on(e, local))
                            and ((key and e.get("key") == key) or fingerprint(e["text"]) == fp)), None)
            now = time.time()
            archived_old = bool(old) and any(e is old for e in doc["archived"])
            if replaces:
                new_scope = scope or (old.get("scope", "global") if old else "global")
                new_worker = (None if protected else local if per_worker
                              else old.get("worker") if old else None)
                targets = self._replacement_targets(
                    doc, replaces, text=text, protected=protected, scope=new_scope,
                    worker=new_worker, self_id=old["id"] if old else None, receipt=receipt)
            else:
                targets = []
            if old and old.get("protected") and not protected:
                if (durable and not old.get("conflicted") and old["text"] == text
                        and not self._metadata_differs(old, when, always)):
                    if targets:
                        self._supersede(doc, old, targets)
                        self._maintain(doc, keep=old["id"])
                        receipt["replaced"] = self._replaced(old, targets)
                        self._write(doc)
                    return self._entry(old)  # already saved at the user's request
                if durable:
                    raise ValueError("This fact was saved at the user's request; an agent cannot "
                                     "rewrite it unless the user asked for the change")
                raise ValueError("Background learning cannot rewrite an explicitly saved fact")
            # A revision guards a rewrite. A key-addressed correction must have
            # resolved above; an unkeyed new save has no slot to compare.
            if expected_revision is not None and old and old.get("revision", 1) != expected_revision:
                raise ValueError("Memory changed since it was read; recall it before retrying")
            # An owner correction of either endpoint resolves the displayed pair.
            resolves = bool(old and memory_id and protected and expected_revision is not None
                            and (old.get("superseded_by") or any(
                                e.get("superseded_by") == global_id(old)
                                and not e.get("supersession_confirmed") for e in all_entries)))
            if old is None and require_when and when is None:
                raise ValueError("when is required for a new memory: the situation in which a "
                                 "future agent should load it (for example 'When choosing a "
                                 "model for a subagent'), not a summary; nothing was saved")
            # The learner and harvests never downgrade an agent's durable fact.
            is_durable = not protected and bool(durable or (old and old.get("durable")))
            if (old and not resolves and not old.get("conflicted") and old["text"] == text
                    and not targets and self._metadata_differs(old, when, always)
                    and old.get("protected", False) == protected
                    and (protected or bool(old.get("durable")) == is_durable)):
                # Same value, new when-line or always flag: a metadata edit.
                # A new revision (replicas must see it), not a new value, so
                # updated_at and history stay as they are.
                if protected and expected_revision is None:
                    raise ValueError("Recall the existing fact and supply its revision before "
                                     f"changing it ([{old['id']} r{old.get('revision', 1)}])")
                if expected_revision is not None and old.get("revision", 1) != expected_revision:
                    raise ValueError("Memory changed since it was read; recall it before retrying")
                if when is not None:
                    old["when"] = when
                if always is not None:
                    if always:
                        self._check_always_budget(doc, old, exclude=())
                        old["always"] = True
                    else:
                        old.pop("always", None)
                self._bump_metadata(doc, old)
                self._maintain(doc, keep=old["id"])
                self._write(doc)
                return self._entry(old)
            if (old and not resolves and not old.get("conflicted") and old["text"] == text
                    and old.get("protected", False) == protected and bool(old.get("durable")) == is_durable):
                if not (protected or durable):
                    if archived_old and not old.get("retired_at") and old.get("expires_at", now + 1) <= now:
                        # Observed again after it expired into the archive:
                        # it is current again, with a fresh lifetime.
                        old["expires_at"] = now + lifetime(old)
                        doc["archived"] = [e for e in doc["archived"] if e is not old]
                        doc["entries"].append(old)
                        self._maintain(doc, keep=old["id"])
                        self._write(doc)
                    return self._entry(old)
                if old.get("superseded_by"):
                    if targets:
                        raise ValueError(f"[{old['id']}] was itself replaced by "
                                         f"[{old['superseded_by']}]; correct that fact instead")
                    return self._entry(old)  # an observation cannot revive a known stale value
                # Saving the same fact again is a use: renew a durable fact's
                # lifetime and bring an archived one back. No new revision.
                if durable:
                    old["expires_at"] = max(old.get("expires_at", 0), now + lifetime(old))
                old["last_recalled_at"] = now
                if archived_old:
                    doc["archived"] = [e for e in doc["archived"] if e is not old]
                    doc["entries"].append(old)
                if old.pop("retired_at", None):
                    # Undoing a retirement is an edit replicas must see, or
                    # their retired copy would win the next merge.
                    old.pop("retired_reason", None)
                    clock = dict(old.get("clock") or {old["origin"]: old.get("revision", 1)})
                    clock[doc["store_id"]] = clock.get(doc["store_id"], 0) + 1
                    old.update(clock=clock, revision=old.get("revision", 1) + 1)
                if targets:
                    self._supersede(doc, old, targets)
                self._maintain(doc, keep=old["id"])
                receipt["replaced"] = self._replaced(old, targets)
                self._write(doc)
                return self._entry(old)
            if old and old.get("protected") and expected_revision is None:
                ref = f"[{old['id']} r{old.get('revision', 1)}]"
                fix = f"pass memory_id={old['id']} expected_revision={old.get('revision', 1)}"
                if key and old.get("key") == key:
                    # One current value per slot: name it so one retry updates it.
                    raise ValueError(f"{key} exists as {ref}; {fix} to update it, "
                                     "folding in what still holds")
                raise ValueError("Recall the existing fact and supply its revision before "
                                 f"correcting it ({ref}; {fix})")
            if protected and not (old and old.get("protected")):
                # A new user-saved fact (or one promoted from agent-saved)
                # needs a free slot. Corrections of a user-saved fact do not.
                if sum(1 for e in all_entries if e.get("protected")) >= MAX_EXPLICIT:
                    raise ValueError(EXPLICIT_FULL_MESSAGE.format(cap=MAX_EXPLICIT))
            entry = dict(old) if old else {
                "id": f"m{doc['next_id']}", "origin": doc["store_id"],
                "created_at": now, "revision": 0, "uses": 0,
            }
            if not old:
                doc["next_id"] += 1
            history = list(entry.get("history", []))
            if old and old["text"] != text:
                previous = {"text": old["text"], "at": now, "revision": old.get("revision", 1)}
                if old.get("when"):
                    previous["when"] = old["when"]
                history.append(previous)
            clock = dict(entry.get("clock") or {entry["origin"]: entry["revision"]})
            slot = key or entry.get("key")
            if old and old.get("conflicted") and protected and slot:
                aliases = [e for e in all_entries if e["id"] != old["id"] and e.get("key") == slot
                           and e.get("scope", "global") == entry.get("scope", "global")]
                for alias in aliases:
                    for actor, counter in (alias.get("clock") or {alias["origin"]: alias.get("revision", 1)}).items():
                        clock[actor] = max(clock.get(actor, 0), counter)
                    doc["forgotten"] = barrier_add(doc.get("forgotten", ""), "id:" + global_id(alias))
                alias_ids = {e["id"] for e in aliases}
                doc["entries"] = [e for e in doc["entries"] if e["id"] not in alias_ids]
                doc["archived"] = [e for e in doc["archived"] if e["id"] not in alias_ids]
            clock[doc["store_id"]] = clock.get(doc["store_id"], 0) + 1
            if len(clock) > 64:
                raise ValueError("Memory has too many concurrent writers; no correction was saved")
            entry.update(clock=clock, alternatives=[], text=text,
                         updated_at=self._write_time(doc, now), revision=entry["revision"] + 1,
                         scope=scope or entry.get("scope", "global"),
                         provenance=provenance or entry.get("provenance", "session"),
                         protected=protected, conflicted=False, key=key or entry.get("key"),
                         history=history[-MAX_HISTORY:], source_session=source_session or entry.get("source_session"),
                         expires_at=0 if protected else (now + DURABLE_TTL if is_durable
                                                         else entry.get("expires_at", now + INFERRED_TTL)))
            # A rewrite of a fact that already expired into the archive is a
            # new observation, not a continuation of the old lifetime.
            if not protected and entry["expires_at"] and entry["expires_at"] <= now:
                entry["expires_at"] = now + INFERRED_TTL
            # A rewrite is a new value: it lives again even if curation had
            # retired the old one.
            entry.pop("retired_at", None)
            entry.pop("retired_reason", None)
            if resolves:
                entry.pop("superseded_by", None)
                entry.pop("supersession_confirmed", None)
            if per_worker and not protected:
                entry["worker"] = local
            elif protected:
                entry.pop("worker", None)  # what the user states is shared
            if not protected and not entry["expires_at"]:
                entry["expires_at"] = now + INFERRED_TTL
            if is_durable:
                entry.update(durable=True, saved_by="agent")
            if not protected:
                entry["expires_at"] = min(entry["expires_at"], now + lifetime(entry))
            if not is_durable:
                entry.pop("durable", None)
                if protected:
                    entry["saved_by"] = "user"
                else:
                    entry.pop("saved_by", None)
            if when is not None:
                entry["when"] = when
            if always is not None:
                entry["always"] = always
            if not protected or not entry.get("always"):
                entry.pop("always", None)  # only an owner-saved fact applies always
            elif (not old or not old.get("always") or not old.get("protected")
                  or old["text"] != text or old.get("scope", "global") != entry["scope"]):
                self._check_always_budget(doc, entry, exclude={e["id"] for e in targets})
            present = any(e["id"] == entry["id"] for e in doc["entries"])
            doc["entries"] = [entry if e["id"] == entry["id"] else e for e in doc["entries"]]
            if not present:
                doc["entries"].append(entry)
            doc["archived"] = [e for e in doc["archived"] if e["id"] != entry["id"]]
            if per_worker and entry.get("key"):
                # The copy older versions shared between workers under this
                # key goes to the archive, so it stops being shown everywhere.
                for shared in [e for e in doc["entries"] if not e.get("worker") and e is not entry
                               and e.get("key") == entry["key"]
                               and e.get("scope", "global") == entry["scope"]
                               and e.get("provenance") == "tool"
                               and not e.get("protected") and not e.get("durable")]:
                    self._retire_entry(doc, shared, "per-worker")
            if resolves:
                for previous in doc["entries"] + doc["archived"]:
                    if previous.get("superseded_by") == global_id(entry):
                        previous["supersession_confirmed"] = True
                        previous.pop("conflicted", None)
                        self._bump_metadata(doc, previous)
            if targets:
                self._supersede(doc, entry, targets)
            self._apply_supersessions(doc, [entry])
            self._maintain(doc, keep=entry["id"])
            receipt["replaced"] = self._replaced(entry, targets)
            if old is None and (protected or is_durable):
                receipt["similar"] = self._similar(doc, entry, {e["id"] for e in targets},
                                                   correction_project)
            self._write(doc)
            return self._entry(entry)

    @staticmethod
    def _entry(entry):
        from fluid.codemode.skills import MemoryEntry
        allowed = {f.name for f in fields(MemoryEntry)}
        return MemoryEntry(**{k: v for k, v in entry.items() if k in allowed})

    @staticmethod
    def _write_time(doc: dict, now: float) -> float:
        """``now``, or the next instant after the newest write this store holds.

        Supersession treats only a strictly later fact as a replacement. The
        Windows clock moves in ~16 ms steps, so two saves in one step would
        tie and the newer one would never replace the older.
        """
        newest = max((float(e.get("updated_at") or 0) for e in doc["entries"] + doc["archived"]),
                     default=0.0)
        return now if now > newest else math.nextafter(newest, math.inf)

    @staticmethod
    def _bump_metadata(doc, entry):
        clock = dict(entry.get("clock") or {entry["origin"]: entry.get("revision", 1)})
        clock[doc["store_id"]] = clock.get(doc["store_id"], 0) + 1
        if len(clock) > 64:
            raise ValueError("Memory has too many concurrent writers; no supersession was saved")
        entry.update(clock=clock, revision=entry.get("revision", 1) + 1)

    def _apply_supersessions(self, doc, candidates=None):
        """Link clear replacements without changing/deleting predecessor text."""
        from fluid.codemode.supersession import replacement_evidence, has_replacement_signal
        from fluid.context.screening import screen_findings
        changed = []
        # Older linked records are bridges for partially repaired legacy
        # chains; direct edges keep proposal history recoverable on withdrawal.
        current = sorted(candidates if candidates is not None else doc["entries"] + [
            e for e in doc["archived"] if e.get("superseded_by")],
                         key=lambda e: (e.get("updated_at", 0), e["id"]))
        for new in current:
            if (not has_replacement_signal(new["text"]) or new.get("conflicted")
                    or (candidates is not None and new.get("superseded_by")) or screen_findings(new["text"])):
                continue
            for old in list(doc["entries"]):
                if (old.get("conflicted") or (old.get("protected") and not new.get("protected"))
                        or not replacement_evidence(old, new)):
                    continue
                old["superseded_by"] = global_id(new)
                # Both are owner-saved and the successor explicitly replaces
                # the old value: no need to ask the owner the same question.
                old["supersession_confirmed"] = bool(new.get("protected"))
                if old.get("protected"):
                    self._bump_metadata(doc, old)
                else:
                    self._retire_entry(doc, old, "superseded")
                changed.append(old["id"])
        return changed

    @staticmethod
    def _replacement_targets(doc, replaces, *, text, protected, scope, worker, self_id, receipt):
        """Resolve Remember(replaces=[...]) before anything is written.

        The rules mirror what :meth:`_repair_supersession_links` keeps, so a
        link made here is not voided on the next maintenance pass: a
        compatible scope (same scope, or a global save over a project fact),
        the same worker, and an owner-saved target only for an owner-saved
        replacement. Any refusal raises, so the whole save is refused.
        """
        from fluid.context.screening import screen_findings
        if screen_findings(text):
            raise ValueError("This text matches an injection pattern, so it cannot replace "
                             "saved facts")
        local = doc["store_id"]
        records = doc["entries"] + doc["archived"]
        targets = []
        for ref in replaces:
            old = next((e for e in records if e["id"] == ref or global_id(e) == ref), None)
            if old is None or not visible_on(old, local):
                raise ValueError(f"replaces names an unknown memory ID [{ref}]; "
                                 "Recall it and retry")
            if old["id"] == self_id:
                raise ValueError("replaces cannot name the fact being saved")
            if old.get("protected") and not protected:
                raise ValueError(
                    f"[{old['id']}] was saved at the user's request; replace it only when the "
                    "user asked for the change (user_requested=true). Nothing was saved")
            old_scope = old.get("scope", "global")
            if old_scope != scope and not (scope == "global" and old_scope.startswith("project:")):
                raise ValueError(f"[{old['id']}] is scoped to {old_scope}; a fact scoped to "
                                 f"{scope} cannot replace it. Nothing was saved")
            if old.get("worker") != worker:
                raise ValueError(f"[{old['id']}] describes one worker's environment; correct it "
                                 "with memory_id instead. Nothing was saved")
            if old.get("superseded_by") and (old.get("supersession_confirmed")
                                             or not old.get("protected")):
                receipt["skipped"].append(f"[{old['id']}] was already replaced")
                continue
            targets.append(old)
        return targets

    def _supersede(self, doc, new, targets):
        """Archive ``targets`` as confirmed predecessors of ``new``.

        Not a delete: no tombstone or deletion barrier, text and history
        stay, Recall finds them labelled superseded, and Forget of ``new``
        brings them back (:meth:`_repair_supersession_links`). The retirement
        bumps each clock, so replicas take the archived copy over their live one.
        """
        gid = global_id(new)
        if new.get("superseded_by") in {global_id(t) for t in targets}:
            new.pop("superseded_by", None)  # the new save is current; no cycle
            new.pop("supersession_confirmed", None)
        held = {id(e) for e in doc["entries"] + doc["archived"]}
        for old in targets:
            if id(old) not in held:
                continue  # folded into this save as a conflicted alias
            old.update(superseded_by=gid, supersession_confirmed=True)
            old.pop("conflicted", None)  # the owner's answer resolves it
            self._retire_entry(doc, old, "superseded")

    @staticmethod
    def _replaced(new, targets):
        """IDs whose link to ``new`` survived maintenance (what the receipt claims)."""
        gid = global_id(new)
        return [t["id"] for t in targets if t.get("superseded_by") == gid]

    @staticmethod
    def _metadata_differs(old, when, always):
        return ((when is not None and when != old.get("when"))
                or (always is not None and always != bool(old.get("always"))))

    @staticmethod
    def _check_always_budget(doc, entry, exclude):
        """Refuse an ALWAYS memory that would overflow ALWAYS_CHARS.

        The budget is per session view: global plus one project's facts. A
        project fact counts against its own project; a global one against
        the fullest project. The message lists the ALWAYS memories it shares
        the budget with (id and when-line, never text) so the agent can ask
        the owner which to drop.
        """
        local = doc["store_id"]
        others = [e for e in doc["entries"]
                  if always_applies(e) and e["id"] != entry["id"] and e["id"] not in exclude
                  and visible_on(e, local)]
        scope = entry.get("scope", "global")

        def view(project):
            return [e for e in others if e.get("scope", "global") in ("global", project)]
        if scope == "global":
            projects = {e.get("scope") for e in others} - {"global"}
            shared = max((view(p) for p in projects), key=lambda v: sum(len(e["text"]) for e in v),
                         default=view("global"))
        else:
            shared = view(scope)
        total = sum(len(e["text"]) for e in shared) + len(entry["text"])
        if total <= ALWAYS_CHARS:
            return
        listed = "; ".join(f"[{e['id']}] {when_line(e)}" for e in shared) or "none"
        raise ValueError(
            f"ALWAYS memories in this scope share {ALWAYS_CHARS} characters and this one would "
            f"bring them to {total}; nothing was saved. Ask the owner which to drop or shorten "
            f"(save it again with always=false), then retry. Current ALWAYS memories: {listed}")

    @staticmethod
    def _similar(doc, new, exclude, project):
        """Up to MAX_SIMILAR live facts that look like the same subject as ``new``.

        Only facts ``new`` could replace (same scope, or the session's project
        under a global save) on this worker. A hint for the receipt; nothing
        is changed.
        """
        from fluid.codemode.supersession import subject_terms

        def terms(text):
            return {w for w in subject_terms(text) if not w.isdigit() and w not in _HINT_NOISE}
        mine = terms(new["text"])
        scope = new.get("scope", "global")
        local = doc["store_id"]
        hits = []
        for e in doc["entries"]:
            if (e is new or e["id"] in exclude or e.get("superseded_by") or e.get("conflicted")
                    or not visible_on(e, local)
                    or not (e.get("scope", "global") == scope
                            or (scope == "global" and project and e.get("scope") == project))):
                continue
            theirs = terms(e["text"])
            shared = len(mine & theirs)
            ratio = shared / max(1, min(len(mine), len(theirs)))
            if shared >= SIMILAR_MIN_SHARED and ratio >= SIMILAR_MIN_RATIO:
                hits.append((-shared, -ratio, e["id"], e))
        return [{"id": e["id"], "revision": e.get("revision", 1), "text": e["text"]}
                for *_, e in sorted(hits, key=lambda h: h[:3])[:MAX_SIMILAR]]

    def annotate(self, memory_id, *, when, expected_revision):
        """Write the when-line of one fact, any tier, without touching its
        text, tier, always flag or lifetime (a backfill or curator path).

        Revision-guarded like a correction; bumps the clock so replicas take
        it. Live or archived facts on this worker. Returns the entry."""
        when = validate_when(when)
        with file_transaction(self.lock):
            doc = self._prepare()
            old = next((e for e in doc["entries"] + doc["archived"] if e["id"] == memory_id), None)
            if old is None or not visible_on(old, doc["store_id"]):
                raise ValueError("Unknown memory ID")
            if old.get("revision", 1) != expected_revision:
                raise ValueError("Memory changed since it was read; recall it before retrying")
            if old.get("when") != when:
                old["when"] = when
                self._bump_metadata(doc, old)
                self._write(doc)
            return self._entry(old)

    def propose_supersession(self, old_id, new_id, *, old_revision, new_revision,
                             protected_conflict=False):
        """Apply a learner/curator proposal only against its exact fact snapshot.

        A user-kept (protected) old fact is refused unless
        ``protected_conflict`` is set and the new fact is user-kept too: the
        pair is then linked unconfirmed, both texts stay as they are, and the
        snapshot asks the owner to resolve it (see preferences.py)."""
        from fluid.codemode.supersession import replacement_evidence
        from fluid.context.screening import screen_findings
        with file_transaction(self.lock):
            doc = self._prepare()
            entries = {e["id"]: e for e in doc["entries"]}
            old, new = entries.get(old_id), entries.get(new_id)
            if old is None or new is None:
                return False
            if old.get("revision", 1) != old_revision or new.get("revision", 1) != new_revision:
                raise ValueError("Memory changed since the supersession proposal was read")
            if old.get("protected") and not (protected_conflict and new.get("protected")):
                return False
            if (old.get("conflicted") or new.get("conflicted") or new.get("superseded_by")
                    or screen_findings(new["text"]) or not replacement_evidence(old, new, proposed=True)):
                return False
            old["superseded_by"] = global_id(new)
            old["supersession_confirmed"] = False
            if old.get("protected"):
                self._bump_metadata(doc, old)
            else:
                self._retire_entry(doc, old, "superseded")
            self._maintain(doc)
            self._write(doc)
            return True

    def reconcile_supersessions(self):
        """Deterministic curation over existing facts; no inference call."""
        with file_transaction(self.lock):
            doc = self._prepare()
            changed = self._apply_supersessions(doc)
            if changed:
                self._maintain(doc)
                self._write(doc)
            return changed

    def forget(self, memory_id, *, explicit=False, expected_revision=None):
        with file_transaction(self.lock):
            doc = self._prepare()
            old = next((e for e in doc["entries"] + doc["archived"] if e["id"] == memory_id), None)
            if old is None:
                return False
            if old.get("protected") and not explicit:
                return False
            if expected_revision is not None and old.get("revision", 1) != expected_revision:
                raise ValueError("Memory changed since it was read; recall it before forgetting")
            tombstone = {"id": old["id"], "origin": old.get("origin", doc["store_id"]),
                         "revision": old.get("revision", 1) + 1, "key": old.get("key"),
                         "scope": old.get("scope", "global"), "deleted_at": time.time(),
                         "fingerprints": [fingerprint(h["text"]) for h in old.get("history", [])] + [fingerprint(old["text"])],
                         "source_session": old.get("source_session")}
            if old.get("superseded_by"):
                # ID-only bridge: no forgotten text is retained here.
                tombstone["superseded_by"] = old["superseded_by"]
            doc["tombstones"].append(tombstone)
            barrier = barrier_add(doc.get("forgotten", ""), "id:" + global_id(tombstone))
            for fp in tombstone["fingerprints"]:
                barrier = barrier_add(barrier, "text:" + fp)
            if tombstone.get("key"):
                barrier = barrier_add(barrier, "key:" + tombstone["scope"] + ":" + tombstone["key"])
            doc["forgotten"] = barrier
            doc["tombstones"] = doc["tombstones"][-MAX_TOMBSTONES:]
            for bucket in ("entries", "archived"):
                doc[bucket] = [e for e in doc[bucket] if e["id"] != memory_id]
            if explicit:
                # Journal evidence older than this is purged and never shown
                # to the learner again. Only a user Forget earns that; a
                # learner delete setting it would let the next maintenance
                # sweep wipe the journal anyway.
                doc["forgotten_before"] = time.time()
            self._maintain(doc)
            self._write(doc)
            return True

    def retire(self, memory_id, *, reason, expected_revision=None):
        """Move one unprotected fact into the archive, where Recall still finds it.

        Automatic curation (the learner, consolidation, health repairs) uses
        this instead of :meth:`forget`: no deletion barrier, nothing purged,
        and a later save of the same fact can bring it back. The clock bump
        makes the retirement win over a replica's older live copy, and
        ``retired_at`` keeps a merge from moving it back into the live list.
        A fact the user asked to keep is never retired.
        """
        with file_transaction(self.lock):
            doc = self._prepare()
            old = next((e for e in doc["entries"] + doc["archived"] if e["id"] == memory_id), None)
            if old is None or old.get("protected") or old.get("retired_at"):
                return False
            if expected_revision is not None and old.get("revision", 1) != expected_revision:
                raise ValueError("Memory changed since it was read; recall it before retiring it")
            self._retire_entry(doc, old, reason)
            self._maintain(doc)
            self._write(doc)
            return True

    def _retire_entry(self, doc, old, reason):
        clock = dict(old.get("clock") or {old.get("origin", doc["store_id"]): old.get("revision", 1)})
        clock[doc["store_id"]] = clock.get(doc["store_id"], 0) + 1
        if len(clock) > 64:
            raise ValueError("Memory has too many concurrent writers; nothing was retired")
        old.update(clock=clock, revision=old.get("revision", 1) + 1,
                   retired_at=time.time(), retired_reason=str(reason)[:40])
        doc["entries"] = [e for e in doc["entries"] if e is not old]
        if not any(e is old for e in doc["archived"]):
            doc["archived"].append(old)
        self._repair_supersession_links(doc)

    def note_recalled(self, ids):
        if not ids:
            return
        with file_transaction(self.lock):
            doc = self._prepare()
            now = time.time()
            for e in doc["entries"]:
                if e["id"] in ids:
                    e["uses"] = min(1_000_000, e.get("uses", 0) + 1)
                    e["last_recalled_at"] = now
                    # A fact that is still being looked up is not stale.
                    if not e.get("protected"):
                        e["expires_at"] = max(e.get("expires_at", 0), now + lifetime(e))
            self._write(doc)

    def export(self):
        doc = self.snapshot()
        return {"schema": SCHEMA, "owner": self.owner, "store_id": doc["store_id"],
                "entries": doc["entries"] + doc["archived"], "tombstones": doc["tombstones"],
                "forgotten": doc.get("forgotten", "")}

    def merge(self, snapshot):
        if snapshot.get("schema") != SCHEMA or snapshot.get("owner") != self.owner:
            raise ValueError("Invalid memory replica owner or schema")
        if len(json.dumps(snapshot).encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("Memory replica exceeds its byte budget")
        with file_transaction(self.lock):
            doc = self._prepare()
            # A namespace is stable across republishing. Imported records keep
            # their origin rather than acquiring every intermediate worker ID.
            def ident(e):
                origin = e.get("origin") or snapshot["store_id"]
                raw_id = e.get("id")
                if not isinstance(origin, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", origin):
                    raise ValueError("Invalid memory origin")
                if not isinstance(raw_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", raw_id):
                    raise ValueError("Invalid memory ID")
                local = raw_id.split(":")[-1]
                return local if origin == doc["store_id"] else f"{origin}:{local}"
            barrier_contains(snapshot.get("forgotten", ""), "validate")
            a = bytes.fromhex(doc.get("forgotten", "")) or bytes(BLOOM_BYTES)
            b = bytes.fromhex(snapshot.get("forgotten", "")) or bytes(BLOOM_BYTES)
            doc["forgotten"] = bytes(x | y for x, y in zip(a, b)).hex()
            tombstones = {t["id"]: t for t in doc["tombstones"]}
            for raw in snapshot.get("tombstones", []):
                t = dict(raw, id=ident(raw))
                if t.get("superseded_by") is not None and not (
                        isinstance(t["superseded_by"], str)
                        and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,160}", t["superseded_by"])):
                    raise ValueError("Invalid forgotten memory supersession link")
                old = tombstones.get(t["id"])
                if not old or t.get("revision", 0) > old.get("revision", 0):
                    # Never retain supplied plaintext in deletion metadata.
                    tombstones[t["id"]] = {k: t.get(k) for k in ("id", "origin", "revision", "key", "scope", "deleted_at", "fingerprints", "source_session", "superseded_by")}
            entries = {e["id"]: e for e in doc["entries"] + doc["archived"]}
            for raw in snapshot.get("entries", []):
                entry = dict(raw, id=ident(raw))
                entry["text"] = validate_text(entry.get("text"))
                if not isinstance(entry.get("scope", "global"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", entry.get("scope", "global")):
                    raise ValueError("Invalid memory scope")
                if entry.get("key") is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", str(entry["key"])):
                    raise ValueError("Invalid memory key")
                if entry.get("provenance") not in ("user", "session", "manual", "tool"):
                    raise ValueError("Invalid memory provenance")
                if entry.get("superseded_by") is not None and not (
                        isinstance(entry["superseded_by"], str)
                        and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}:[A-Za-z0-9_.-]{1,160}", entry["superseded_by"])):
                    raise ValueError("Invalid memory supersession link")
                if type(entry.get("supersession_confirmed", False)) is not bool:
                    raise ValueError("Invalid memory supersession confirmation")
                if entry.get("worker") is not None and not (
                        isinstance(entry["worker"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", entry["worker"])):
                    raise ValueError("Invalid memory worker")
                if entry.get("when") is not None:
                    try:
                        entry["when"] = validate_when(entry["when"])
                    except ValueError:
                        # An unusable when-line is not worth refusing the
                        # replica for: the fact is listed by its text instead.
                        entry.pop("when", None)
                if type(entry.get("always", False)) is not bool:
                    raise ValueError("Invalid memory always flag")
                if not entry.get("always"):
                    entry.pop("always", None)
                old = entries.get(entry["id"])
                incoming_clock = entry.get("clock") or {entry["origin"]: entry.get("revision", 1)}
                if not isinstance(incoming_clock, dict) or len(incoming_clock) > 64 or any(type(v) is not int or v < 0 for v in incoming_clock.values()):
                    raise ValueError("Invalid or oversized memory revision clock")
                entry["clock"] = incoming_clock
                if not old:
                    entries[entry["id"]] = entry
                    continue
                local_clock = old.get("clock") or {old["origin"]: old.get("revision", 1)}
                actors = set(local_clock) | set(incoming_clock)
                newer = all(incoming_clock.get(k, 0) >= local_clock.get(k, 0) for k in actors)
                older = all(local_clock.get(k, 0) >= incoming_clock.get(k, 0) for k in actors)
                if newer and not older:
                    entries[entry["id"]] = entry
                elif (not newer and not older) or (newer and older and entry["text"] != old["text"]):
                    # Independent deterministic reconciliation is the same
                    # claim, not a concurrent edit to the fact's value.
                    claim = ("text", "scope", "key", "worker", "provenance", "protected", "durable", "superseded_by", "retired_reason")
                    same_value = (all(old.get(k) == entry.get(k) for k in claim)
                                  and bool(old.get("retired_at")) == bool(entry.get("retired_at")))
                    same_claim = same_value and all(old.get(k) == entry.get(k) for k in ("when", "always"))
                    if same_value and not same_claim:
                        # Concurrent when-line/always edits of one value are
                        # metadata, not a conflict: every replica keeps the
                        # same side (higher revision, then the larger when).
                        winner = max((old, entry), key=lambda e: (
                            e.get("revision", 1), str(e.get("when") or ""), bool(e.get("always"))))
                        for k in ("when", "always"):
                            if winner.get(k):
                                old[k] = winner[k]
                            else:
                                old.pop(k, None)
                        same_claim = True
                    if same_claim:
                        old["clock"] = {k: max(local_clock.get(k, 0), incoming_clock.get(k, 0)) for k in actors}
                        old["revision"] = max(old.get("revision", 1), entry.get("revision", 1))
                        old["supersession_confirmed"] = bool(old.get("supersession_confirmed") or entry.get("supersession_confirmed"))
                        old["conflicted"] = bool(old.get("conflicted") or entry.get("conflicted")) and not old["supersession_confirmed"]
                        old["alternatives"] = sorted(set(old.get("alternatives", []) + entry.get("alternatives", [])))[:2]
                        continue
                    old["conflicted"] = True
                    old["clock"] = {k: max(local_clock.get(k, 0), incoming_clock.get(k, 0)) for k in actors}
                    old["revision"] = max(old.get("revision", 1), entry.get("revision", 1))
                    old["alternatives"] = sorted(set(old.get("alternatives", []) + [old["text"], entry["text"]]))[:2]
            for key in tombstones:
                # Delete wins over every replay. Restoring creates a new ID;
                # a stale worker cannot resurrect the forgotten record.
                entries.pop(key, None)
            doc["tombstones"] = list(tombstones.values())
            kept = [e for e in entries.values()
                    if not barrier_contains(doc.get("forgotten", ""), "id:" + global_id(e))]
            # A retired fact stays archived on every replica; other archived
            # facts compete for live room again and _maintain re-bounds them.
            doc["entries"] = [e for e in kept if not e.get("retired_at")]
            doc["archived"] = [e for e in kept if e.get("retired_at")]
            self._repair_supersession_links(doc)
            slots = {}
            for e in doc["entries"]:
                if e.get("key") and not e.get("superseded_by"):
                    # Each worker's copy of an environment fact is its own slot.
                    slots.setdefault((e.get("scope", "global"), e["key"], e.get("worker") or ""), []).append(e)
            for group in slots.values():
                if len({e["text"] for e in group}) > 1:
                    for e in group:
                        e["conflicted"] = True
            self._maintain(doc)
            self._write(doc)

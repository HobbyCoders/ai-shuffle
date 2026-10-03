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
import re
import time
import uuid
from pathlib import Path

from fluid.codemode.transactions import file_transaction

_log = logging.getLogger(__name__)

# Three tiers, none of which can refuse a save for lack of room:
#
# * explicit (``protected``): the user asked for it or corrected a fact. No
#   TTL and never touched by the learner; past MAX_EXPLICIT live entries the
#   least useful moves to the searchable archive rather than refusing a save.
# * durable (``durable``): an agent decided a fact was worth keeping
#   (Remember without ``user_requested``). Longer TTL than inferred, renewed
#   when recalled or re-saved, curated by consolidation, and archived past
#   MAX_DURABLE. The store filled its 256-slot explicit quota in three weeks
#   when every agent save was explicit, then refused every new save.
# * inferred: learner output and tool harvests, as before.
MAX_EXPLICIT = 256
MAX_DURABLE = 120
# Archived explicit facts are kept apart from the shared archive so an
# inference flood can never trim a fact the user asked for.
MAX_ARCHIVED_EXPLICIT = 256
DURABLE_TTL = 120 * 86400
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
MAX_PROMPT_CHARS = 8000
# Part of MAX_PROMPT_CHARS that unprotected facts (learner output, env.*
# harvests) claim before protected facts fill the rest. Without it a store of
# protected Remember facts alone hides every learned and harvested fact. Sized
# on the desktop store: its 6 in-scope unprotected facts render at about 2.2k
# chars (median line 306 chars, plus the 80-char "harvested" hint on the
# tool-sourced ones), which 2,000 did not cover.
RESERVED_INFERRED_CHARS = 3000
INFERRED_TTL = 90 * 86400
MAX_INFERENCE_CALLS_PER_DAY = 48
HISTORY_TTL = 30 * 86400
MAX_HISTORY = 2
SCHEMA = 2


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
            grace = VOLATILE_GRACE if volatile(e.get("text")) else MIGRATION_GRACE
            e["expires_at"] = max(activity + lifetime(e), now + grace)
        elif "durable" in e and not isinstance(e["durable"], bool):
            e["durable"] = bool(e["durable"])

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
            # Expiry does not become a forget tombstone. It travels with the
            # fact so replaying an old snapshot cannot renew its lifetime.
            expired = [e for e in doc[bucket] if not e.get("protected") and e.get("expires_at", now + 1) <= now]
            doc[bucket] = [e for e in doc[bucket] if e not in expired]
            if expired:
                self._note_loss(doc, "expired_live" if bucket == "entries" else "expired_archived",
                                len(expired), now)
        live = doc["entries"]
        inferred = [e for e in live if tier(e) == "inferred"]
        ranked = sorted(inferred, key=lambda e: (e.get("id") == keep, e.get("uses", 0), e.get("updated_at", 0)))
        overflow = ranked[:max(0, len(inferred) - MAX_MEMORIES)]
        for name, cap in (("durable", MAX_DURABLE), ("explicit", MAX_EXPLICIT)):
            group = [e for e in live if tier(e) == name]
            ranked = sorted(group, key=lambda e: (e.get("id") == keep, usefulness(e), str(e.get("id"))))
            overflow += ranked[:max(0, len(group) - cap)]
        gone = {id(e) for e in overflow}
        doc["entries"] = [e for e in live if id(e) not in gone]
        archived = doc["archived"] + overflow
        # An archived explicit fact leaves only by an explicit Forget or by
        # MAX_ARCHIVED_EXPLICIT newer ones; the shared archive gives up
        # inferred facts before durable ones, least useful first.
        explicit = sorted((e for e in archived if e.get("protected")), key=lambda e: (usefulness(e), str(e.get("id"))))
        rest = sorted((e for e in archived if not e.get("protected")),
                      key=lambda e: (tier(e) == "durable", usefulness(e) if e.get("durable") else e.get("updated_at", 0), str(e.get("id"))))
        doc["archived"] = (rest[max(0, len(rest) - MAX_ARCHIVED_MEMORIES):]
                           + explicit[max(0, len(explicit) - MAX_ARCHIVED_EXPLICIT):])
        if len(rest) > MAX_ARCHIVED_MEMORIES:
            self._note_loss(doc, "archive_dropped", len(rest) - MAX_ARCHIVED_MEMORIES, now)
        if len(explicit) > MAX_ARCHIVED_EXPLICIT:
            # The one way a user-requested fact leaves without a Forget. It
            # used to be silent; the health check reports it.
            dropped = len(explicit) - MAX_ARCHIVED_EXPLICIT
            self._note_loss(doc, "explicit_dropped", dropped, now)
            _log.warning("Memory store %s dropped %d user-saved fact(s) from a full archive",
                         doc.get("store_id"), dropped)
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

    def claim_inference_call(self, reserve=0):
        """Claim one of the day's inference calls, leaving ``reserve`` unclaimed."""
        with file_transaction(self.lock):
            doc = self._prepare()
            day = int(time.time() // 86400)
            budget = doc.get("inference_budget", {})
            calls = budget.get("calls", 0) if budget.get("day") == day else 0
            if calls >= MAX_INFERENCE_CALLS_PER_DAY - max(0, reserve):
                return False
            doc["inference_budget"] = {"day": day, "calls": calls + 1}
            self._write(doc)
            return True

    def snapshot(self):
        with file_transaction(self.lock):
            return self._prepare()

    def entries(self, archived=False):
        return [self._entry(e) for e in self.snapshot()["archived" if archived else "entries"]]

    def save(self, text, memory_id=None, scope=None, provenance=None, *, protected=False,
             durable=None, key=None, expected_revision=None, source_session=None, restore=False):
        """Save one fact in a tier.

        ``protected`` is the explicit tier (the user asked). ``durable=True``
        is an agent's own save. Neither (the learner, harvests) keeps a
        rewritten fact's existing tier and never downgrades one.
        """
        text = validate_text(text)
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,96}", key)):
            raise ValueError("Invalid memory key")
        key = key or (canonical_key(text) if scope in (None, "global") else None)
        fp = fingerprint(text)
        with file_transaction(self.lock):
            doc = self._prepare()
            suppressed = barrier_contains(doc.get("forgotten", ""), "text:" + fp)
            if key:
                suppressed = suppressed or barrier_contains(doc.get("forgotten", ""), "key:" + (scope or "global") + ":" + key)
            if suppressed and not (protected and restore):
                raise ValueError("This fact matches a deletion barrier; an explicit restore request is required")
            for t in doc["tombstones"]:
                if fp in t.get("fingerprints", []) or (key and t.get("key") == key and t.get("scope") == (scope or "global")):
                    if not (protected and restore):
                        raise ValueError("This fact was forgotten; an explicit restore request is required")
            all_entries = doc["entries"] + doc["archived"]
            old = next((e for e in all_entries if e["id"] == memory_id), None) if memory_id else None
            if memory_id and old is None:
                raise ValueError("Unknown memory ID; a rewrite cannot insert a new fact")
            if old is None:
                old = next((e for e in all_entries if e.get("scope", "global") == (scope or "global") and
                            ((key and e.get("key") == key) or fingerprint(e["text"]) == fp)), None)
            now = time.time()
            archived_old = bool(old) and any(e is old for e in doc["archived"])
            if old and old.get("protected") and not protected:
                if durable and not old.get("conflicted") and old["text"] == text:
                    return self._entry(old)  # already saved at the user's request
                if durable:
                    raise ValueError("This fact was saved at the user's request; an agent cannot "
                                     "rewrite it unless the user asked for the change")
                raise ValueError("Background learning cannot rewrite an explicitly saved fact")
            # A revision guards a rewrite. With nothing to rewrite (a new fact,
            # no memory ID) it has nothing to compare against: models fill the
            # optional field in anyway, and refusing made every such save fail.
            if expected_revision is not None and old and old.get("revision", 1) != expected_revision:
                raise ValueError("Memory changed since it was read; recall it before retrying")
            # The learner and harvests never downgrade an agent's durable fact.
            is_durable = not protected and bool(durable or (old and old.get("durable")))
            if (old and not old.get("conflicted") and old["text"] == text
                    and old.get("protected", False) == protected and bool(old.get("durable")) == is_durable):
                if not (protected or durable):
                    return self._entry(old)
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
                self._maintain(doc, keep=old["id"])
                self._write(doc)
                return self._entry(old)
            if old and old.get("protected") and expected_revision is None:
                raise ValueError("Recall the existing fact and supply its revision before correcting it")
            entry = dict(old) if old else {
                "id": f"m{doc['next_id']}", "origin": doc["store_id"],
                "created_at": now, "revision": 0, "uses": 0,
            }
            if not old:
                doc["next_id"] += 1
            history = list(entry.get("history", []))
            if old and old["text"] != text:
                history.append({"text": old["text"], "at": now, "revision": old.get("revision", 1)})
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
            entry.update(clock=clock, alternatives=[], text=text, updated_at=now, revision=entry["revision"] + 1,
                         scope=scope or entry.get("scope", "global"),
                         provenance=provenance or entry.get("provenance", "session"),
                         protected=protected, conflicted=False, key=key or entry.get("key"),
                         history=history[-MAX_HISTORY:], source_session=source_session or entry.get("source_session"),
                         expires_at=0 if protected else (now + DURABLE_TTL if is_durable
                                                         else entry.get("expires_at", now + INFERRED_TTL)))
            # A rewrite is a new value: it lives again even if curation had
            # retired the old one.
            entry.pop("retired_at", None)
            entry.pop("retired_reason", None)
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
            present = any(e["id"] == entry["id"] for e in doc["entries"])
            doc["entries"] = [entry if e["id"] == entry["id"] else e for e in doc["entries"]]
            if not present:
                doc["entries"].append(entry)
            doc["archived"] = [e for e in doc["archived"] if e["id"] != entry["id"]]
            self._maintain(doc, keep=entry["id"])
            self._write(doc)
            return self._entry(entry)

    @staticmethod
    def _entry(entry):
        from fluid.codemode.skills import MemoryEntry
        allowed = {f.name for f in fields(MemoryEntry)}
        return MemoryEntry(**{k: v for k, v in entry.items() if k in allowed})

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
            clock = dict(old.get("clock") or {old.get("origin", doc["store_id"]): old.get("revision", 1)})
            clock[doc["store_id"]] = clock.get(doc["store_id"], 0) + 1
            if len(clock) > 64:
                raise ValueError("Memory has too many concurrent writers; nothing was retired")
            old.update(clock=clock, revision=old.get("revision", 1) + 1,
                       retired_at=time.time(), retired_reason=str(reason)[:40])
            doc["entries"] = [e for e in doc["entries"] if e is not old]
            if not any(e is old for e in doc["archived"]):
                doc["archived"].append(old)
            self._maintain(doc)
            self._write(doc)
            return True

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
                old = tombstones.get(t["id"])
                if not old or t.get("revision", 0) > old.get("revision", 0):
                    # Never retain supplied plaintext in deletion metadata.
                    tombstones[t["id"]] = {k: t.get(k) for k in ("id", "origin", "revision", "key", "scope", "deleted_at", "fingerprints", "source_session")}
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
            slots = {}
            for e in doc["entries"]:
                if e.get("key"):
                    slots.setdefault((e.get("scope", "global"), e["key"]), []).append(e)
            for group in slots.values():
                if len({e["text"] for e in group}) > 1:
                    for e in group:
                        e["conflicted"] = True
            self._maintain(doc)
            self._write(doc)

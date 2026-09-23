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
import re
import time
import uuid
from pathlib import Path

from fluid.codemode.transactions import file_transaction

MAX_EXPLICIT = 256
MAX_TOMBSTONES = 256
BLOOM_BYTES = 16 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_PROMPT_CHARS = 8000
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

    def _maintain(self, doc):
        from fluid.codemode.skills import MAX_MEMORIES, MAX_ARCHIVED_MEMORIES
        before = json.dumps(doc, sort_keys=True)
        now = time.time()
        for bucket in ("entries", "archived"):
            doc[bucket] = [e for e in doc[bucket] if isinstance(e, dict) and e.get("id") and e.get("text")]
            for e in doc[bucket]:
                e["history"] = [h for h in e.get("history", []) if h.get("at", 0) > now - HISTORY_TTL][-MAX_HISTORY:]
            # Expiry does not become a forget tombstone. It travels with the
            # fact so replaying an old snapshot cannot renew its lifetime.
            expired = [e for e in doc[bucket] if not e.get("protected") and e.get("expires_at", now + 1) <= now]
            doc[bucket] = [e for e in doc[bucket] if e not in expired]
        inferred = [e for e in doc["entries"] if not e.get("protected")]
        ranked = sorted(inferred, key=lambda e: (e.get("uses", 0), e.get("updated_at", 0)))
        overflow = ranked[:max(0, len(inferred) - MAX_MEMORIES)]
        doc["entries"] = [e for e in doc["entries"] if e not in overflow]
        doc["archived"].extend(overflow)
        doc["archived"] = sorted(doc["archived"], key=lambda e: e.get("updated_at", 0))[-MAX_ARCHIVED_MEMORIES:]
        if sum(bool(e.get("protected")) for e in doc["entries"]) > MAX_EXPLICIT:
            raise ValueError("Explicit memory quota reached; no previous fact was evicted")
        # Compact deletion bodies; the monotonic barrier survives compaction.
        doc["tombstones"] = doc["tombstones"][-MAX_TOMBSTONES:]
        return before != json.dumps(doc, sort_keys=True)

    def reap(self):
        with file_transaction(self.lock):
            doc = self._load()
            before = len(doc["entries"]) + len(doc["archived"])
            after = self._prepare()
            return max(0, before - len(after["entries"]) - len(after["archived"]))

    def claim_inference_call(self):
        with file_transaction(self.lock):
            doc = self._prepare()
            day = int(time.time() // 86400)
            budget = doc.get("inference_budget", {})
            calls = budget.get("calls", 0) if budget.get("day") == day else 0
            if calls >= MAX_INFERENCE_CALLS_PER_DAY:
                return False
            doc["inference_budget"] = {"day": day, "calls": calls + 1}
            self._write(doc)
            return True

    def snapshot(self):
        with file_transaction(self.lock):
            return self._prepare()

    def entries(self, archived=False):
        from fluid.codemode.skills import MemoryEntry
        allowed = {f.name for f in fields(MemoryEntry)}
        return [MemoryEntry(**{k: v for k, v in e.items() if k in allowed})
                for e in self.snapshot()["archived" if archived else "entries"]]

    def save(self, text, memory_id=None, scope=None, provenance=None, *, protected=False,
             key=None, expected_revision=None, source_session=None, restore=False):
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
            if old and old.get("protected") and not protected:
                raise ValueError("Background learning cannot rewrite an explicitly saved fact")
            # A revision guards a rewrite. With nothing to rewrite (a new fact,
            # no memory ID) it has nothing to compare against: models fill the
            # optional field in anyway, and refusing made every such save fail.
            if expected_revision is not None and old and old.get("revision", 1) != expected_revision:
                raise ValueError("Memory changed since it was read; recall it before retrying")
            if old and not old.get("conflicted") and old["text"] == text and old.get("protected", False) == protected:
                return self._entry(old)
            if old and old.get("protected") and expected_revision is None:
                raise ValueError("Recall the existing fact and supply its revision before correcting it")
            now = time.time()
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
                         expires_at=0 if protected else entry.get("expires_at", now + INFERRED_TTL))
            if not protected and not entry["expires_at"]:
                entry["expires_at"] = now + INFERRED_TTL
            present = any(e["id"] == entry["id"] for e in doc["entries"])
            doc["entries"] = [entry if e["id"] == entry["id"] else e for e in doc["entries"]]
            if not present:
                doc["entries"].append(entry)
            doc["archived"] = [e for e in doc["archived"] if e["id"] != entry["id"]]
            self._maintain(doc)
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
            doc["forgotten_before"] = time.time()
            self._write(doc)
            return True

    def note_recalled(self, ids):
        if not ids:
            return
        with file_transaction(self.lock):
            doc = self._prepare()
            for e in doc["entries"]:
                if e["id"] in ids:
                    e["uses"] = min(1_000_000, e.get("uses", 0) + 1)
                    e["last_recalled_at"] = time.time()
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
            for key, t in tombstones.items():
                # Delete wins over every replay. Restoring creates a new ID;
                # a stale worker cannot resurrect the forgotten record.
                entries.pop(key, None)
            doc["tombstones"] = list(tombstones.values())
            doc["entries"], doc["archived"] = [e for e in entries.values()
                if not barrier_contains(doc.get("forgotten", ""), "id:" + global_id(e))], []
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

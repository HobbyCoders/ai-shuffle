"""Replicate memory facts and separately managed procedural skills.

Each worker publishes one small record per fact of each shared store
(``memory-fact:<device>:<owner hash>:<fact hash>``) and one barrier record per
store (``memory-barrier:<device>:<owner hash>``) holding its tombstones and
forget filter. ``FactStore.merge`` is entry-wise, so a payload holding one
entry merges exactly like that entry inside a whole snapshot, and a change to
one fact sends one small record instead of a whole-store copy (#1167).

A missing fact is not a deletion: a fact record this worker no longer exports
is marked deleted and receivers ignore deleted fact records. Forgetting
travels only through the barrier record. All imports use the normal
transaction API.

The whole-store record (``memory-v2:<device>:<owner hash>``) is still written
while another active worker does not advertise :data:`CAPABILITY` in its
signed presence, and marked deleted once every one does. Receivers merge every
live whole-store record, whoever published it, so old and new workers keep
exchanging memory.

Incoming records are committed first and written to the files by a one-shot
post-commit notification. :func:`reconcile_files` re-applies every peer's live
record: once at network start (a crash between the commit and the apply) and
on the next publish pass after an apply hit an I/O error.
Code-mode Python skills live under ``skills_root()``; prose skill folders
(``~/.fluid/skills/<name>/SKILL.md``) ride the same collection through
:mod:`app.network.prose_skills`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from time import monotonic

from app.db.base import get_db
from app.network import engine, prose_skills

log = logging.getLogger(__name__)
CAPABILITY = "memory_fact_records"
FACT_PREFIX = "memory-fact:"
BARRIER_PREFIX = "memory-barrier:"
SNAPSHOT_PREFIX = "memory-v2:"
_SKILL_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}$")
MAX_SKILL_BYTES = 65536
WRITE_QUIET_SECONDS = 0.1
WRITE_MAX_WAIT_SECONDS = 2.0
_write_lock = threading.Lock()
_write_window = None  # first/last committed local change; peer applies never enter it
_reconcile_owed = False  # an apply hit an I/O error; the next publish pass re-applies


def _publish_due() -> bool:
    """Collect a burst until quiet, but never starve a continuously edited store."""
    global _write_window
    with _write_lock:
        if _write_window is None:
            return True
        first, last = _write_window
        if monotonic() < min(last + WRITE_QUIET_SECONDS, first + WRITE_MAX_WAIT_SECONDS):
            return False
        # Writes during staging/publish open the next window, not a lost wake.
        _write_window = None
        return True


def _root() -> Path:
    from app.core.fluid_memory import skills_root
    return skills_root()


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _store(owner="local"):
    from fluid.codemode.skills import SkillStore
    if owner == "local":
        return SkillStore(_root())
    if not isinstance(owner, str) or not owner.startswith("api:") or not owner[4:]:
        raise ValueError("Invalid memory owner")
    path = _root() / ".users" / _hash(owner)
    return SkillStore(path, owner=owner)


def _stores():
    if (_root() / "memories.json").exists():
        yield _store()
    for path in (_root() / ".users").glob("*/memories.json"):
        try:
            owner = json.loads(path.read_text(encoding="utf-8"))["owner"]
            store = _store(owner)
            if store.root == path.parent:
                yield store
        except (ValueError, KeyError, OSError):
            log.warning("Skipped invalid owner memory store")


def install_write_hook() -> None:
    """Bridge Fluid's post-commit hint to a coalesced targeted scanner wake."""
    from fluid.codemode import transactions

    def changed(paths):
        global _write_window
        root = _root().resolve()
        for path in paths:
            path = path.resolve()
            # Only installation/API-owner fact stores and installation Python
            # skills are shared. Project/standalone stores must stay local.
            if (path.parent == root and (path.name == "memories.json" or path.suffix == ".py")) or (
                path.name == "memories.json" and path.parent.parent == root / ".users"
            ):
                now = monotonic()
                with _write_lock:
                    _write_window = (_write_window[0] if _write_window else now, now)
                from app.network import transport
                transport.request_scan(["memory"])
                return

    transactions._change_callback = changed


def refresh_from_files(*, ctx=None) -> None:
    ctx = ctx or engine.context()
    if not ctx:
        return
    with prose_skills.LOCK:
        manifest = prose_skills.load_manifest()
        if _refresh(ctx, manifest):
            prose_skills.save_manifest(manifest)
    if _reconcile_owed:
        try:
            reconcile_files(ctx=ctx)
        except Exception:  # noqa: BLE001 - still owed; publishing must go on
            log.warning("Re-applying memory records failed", exc_info=True)


def reconcile_files(*, ctx=None) -> int:
    """Re-apply every peer's live fact, barrier and whole-store record.

    A per-fact record is sent once, so one the files missed (a crash after
    its commit, a failed write) stays missing until that fact changes again.
    This worker's own records are skipped: its files are their source, and
    merging an older copy back could restore a fact it has since dropped.
    """
    return apply_to_files([], ctx=ctx, reconcile=True)


def _supports(presence_row) -> bool:
    return ((presence_row or {}).get("capabilities") or {}).get(CAPABILITY) == 1


def _snapshot_readers(ctx) -> bool:
    """Whether another active worker may still need the whole-store record.

    Membership defines the set, signed presence the support (the
    ``native_providers.workers_requiring_update`` rule): an unknown or missing
    presence counts as an old worker. Consumers (phones, browsers) do not
    merge fact stores.
    """
    from app.db import network as store
    from app.network import membership
    try:
        presence = engine.all_presence()
        members = store.list_members(include_revoked=False)
    except Exception:  # noqa: BLE001 - unknown means keep the old format too
        log.warning("Could not read member capabilities; keeping whole-store memory records", exc_info=True)
        return True
    return any(member["device_id"] != ctx.device_id
               and (member.get("kind") or membership.KIND_WORKER) == membership.KIND_WORKER
               and not _supports(presence.get(member["device_id"]))
               for member in members)


def _fact_records(ctx, payload, owner_hash):
    """One record per exported entry plus the store's barrier record."""
    head = {"schema": payload["schema"], "owner": payload["owner"], "store_id": payload["store_id"]}
    out = {}
    for entry in payload["entries"]:
        content = json.dumps({**head, "entries": [entry]}, sort_keys=True)
        rid = f"{FACT_PREFIX}{ctx.device_id}:{owner_hash}:{_hash(str(entry['id']))[:24]}"
        out[rid] = ("fact", "global", content, _hash(content))
    barrier = {**head, "entries": [], "tombstones": payload["tombstones"]}
    forgotten = payload.get("forgotten") or ""
    # merge() reads an absent filter as an all-zero one, so omitting it is
    # the same merge and keeps an unforgotten store's barrier tiny.
    if any(bytes.fromhex(forgotten)):
        barrier["forgotten"] = forgotten
    content = json.dumps(barrier, sort_keys=True)
    out[f"{BARRIER_PREFIX}{ctx.device_id}:{owner_hash}"] = ("fact", "global", content, _hash(content))
    return out


def _refresh(ctx, manifest) -> bool:
    root = _root()
    incoming = {}
    exported = set()  # owner hashes whose store was read this pass
    snapshot_readers = None
    for store in (_stores() if root.is_dir() else ()):
        payload = store._facts.export()
        # Usage counters affect local ranking, not fact truth or sync revisions.
        for entry in payload["entries"]:
            entry.pop("uses", None)
            entry.pop("last_recalled_at", None)
        owner_hash = _hash(store._facts.owner)
        exported.add(owner_hash)
        incoming.update(_fact_records(ctx, payload, owner_hash))
        if snapshot_readers is None:
            snapshot_readers = _snapshot_readers(ctx)
        if snapshot_readers:
            content = json.dumps(payload, sort_keys=True)
            rid = f"{SNAPSHOT_PREFIX}{ctx.device_id}:{owner_hash}"
            incoming[rid] = ("fact", "global", content, _hash(content))
    if root.is_dir():
        for path in root.glob("*.py"):
            if path.stem.startswith("_") or not _SKILL_NAME.fullmatch(path.stem):
                continue
            if path.stat().st_size > MAX_SKILL_BYTES:
                continue
            content = path.read_text(encoding="utf-8")
            incoming[f"skill:{path.stem}"] = ("skill", "global", content, _hash(content))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, content_hash, deleted FROM network_memory_records")
        known = {r["id"]: r for r in cur.fetchall()}
        for rid, (kind, scope, content, digest) in incoming.items():
            old = known.get(rid)
            if old and old.get("content_hash") == digest and not old.get("deleted"):
                continue
            cur.execute(
                "INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
                "VALUES (%s, %s, %s, %s, %s, FALSE) ON CONFLICT (id) DO UPDATE SET "
                "kind=EXCLUDED.kind, scope=EXCLUDED.scope, content=EXCLUDED.content, "
                "content_hash=EXCLUDED.content_hash, deleted=FALSE, updated_at=NOW()",
                (rid, kind, scope, content, digest),
            )
        own = [f"{prefix}{ctx.device_id}:" for prefix in (FACT_PREFIX, BARRIER_PREFIX, SNAPSHOT_PREFIX)]
        for rid, old in known.items():
            # Retract this worker's fact, barrier and (once no old worker
            # reads it) whole-store records of stores read this pass. Receivers
            # ignore a deleted memory record: it is not a fact deletion.
            mine = next((rid[len(p):] for p in own if rid.startswith(p)), None)
            if (mine is not None and rid not in incoming and not old.get("deleted")
                    and mine.split(":", 1)[0] in exported):
                cur.execute("UPDATE network_memory_records SET deleted=TRUE, content='', content_hash='', "
                            "updated_at=NOW() WHERE id=%s", (rid,))
        for rid, old in known.items():
            if rid.startswith("skill:") and rid not in incoming and not old.get("deleted") and root.is_dir():
                cur.execute("UPDATE network_memory_records SET deleted=TRUE, updated_at=NOW() WHERE id=%s", (rid,))
        changed = prose_skills.stage_publish(cur, known, manifest)
    return changed


def publish(*, ctx=None) -> int:
    ctx = ctx or engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return 0
    if not _publish_due():
        # The fixed scanner wake alone can cut a slow ten-save burst into
        # several whole-store envelopes. Retry after its next debounce instead
        # of staging any intermediate snapshot (also on a periodic full pass).
        from app.network import transport
        transport.request_scan(["memory"])
        return 0
    refresh_from_files(ctx=ctx)
    try:
        from app.network import authored_plugins
        with get_db() as conn:
            authored_plugins.refresh_from_files(conn.cursor())
    except Exception:  # noqa: BLE001 - drafts must not stop facts and skills
        log.warning("Publishing authored plugin drafts failed", exc_info=True)
    return engine.scan_collection("memory", ctx=ctx)


def apply_to_files(ids: list[str], *, ctx=None, reconcile=False) -> int:
    global _reconcile_owed
    if not (ids or reconcile):
        return 0
    try:
        ctx = ctx or engine.context()
        if not ctx:
            return 0
        if "" in ids or reconcile:
            # A state copy announces the collection, not its records. Unchanged
            # facts would otherwise never reach the files: no later whole-store
            # record carries them. Merging is idempotent. A reconcile (see
            # reconcile_files) leaves out this worker's own records.
            if reconcile:
                _reconcile_owed = False  # set again by any failure below
            own = () if "" in ids else tuple(f"{p}{ctx.device_id}:" for p in
                                             (FACT_PREFIX, BARRIER_PREFIX, SNAPSHOT_PREFIX))
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM network_memory_records WHERE deleted = FALSE AND "
                            "(id LIKE %s OR id LIKE %s OR id LIKE %s)",
                            tuple(p + "%" for p in (FACT_PREFIX, BARRIER_PREFIX, SNAPSHOT_PREFIX)))
                ids = sorted({i for i in ids if i} |
                             {r["id"] for r in cur.fetchall() if not (own and r["id"].startswith(own))})
            if not ids:
                return 0
        from fluid.codemode.transactions import suppress_changes
        with suppress_changes(), prose_skills.LOCK:
            manifest = prose_skills.load_manifest()
            before = json.dumps(manifest, sort_keys=True)
            applied = _apply(ids, ctx, manifest)
            if json.dumps(manifest, sort_keys=True) != before:
                prose_skills.save_manifest(manifest)
    except Exception:
        _reconcile_owed = True
        raise
    return applied


def _rejected(rid, exc) -> None:
    """A bad record is refused for good; an I/O failure is retried (#1167)."""
    global _reconcile_owed
    if isinstance(exc, OSError):
        _reconcile_owed = True
    log.error("Memory replica rejected: %s", rid, exc_info=exc)


def _apply(ids, ctx, manifest) -> int:
    applied = 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM network_memory_records WHERE id IN (%s)" % ",".join(["%s"] * len(ids)), tuple(ids))
        records = list(cur.fetchall())
    from app.network import authored_plugins
    authored = [r for r in records if authored_plugins.is_record(r["id"])]
    if authored:
        try:
            applied += authored_plugins.apply_records(authored)
        except Exception:  # noqa: BLE001 - drafts must not stop facts and skills
            log.exception("Applying authored plugin records failed")
    records = [r for r in records if not authored_plugins.is_record(r["id"])]
    from fluid.codemode.skills import atomic_write_text
    fact_groups = {}
    for record in records:
        rid = record["id"]
        try:
            if record.get("kind") == prose_skills.KIND:
                if rid.startswith(prose_skills.PREFIX) and prose_skills.apply_record(record, manifest):
                    applied += 1
                continue
            if record.get("kind") == "skill":
                name = rid.removeprefix("skill:")
                if not _SKILL_NAME.fullmatch(name):
                    continue
                path = _root() / f"{name}.py"
                if record.get("deleted"):
                    path.unlink(missing_ok=True)
                else:
                    source = record.get("content") or ""
                    if len(source.encode()) > MAX_SKILL_BYTES:
                        continue
                    if path.exists() and path.read_text(encoding="utf-8") == source:
                        continue
                    atomic_write_text(path, source)
                applied += 1
                continue
            if rid.startswith((FACT_PREFIX, BARRIER_PREFIX)):
                # A deleted fact record means only "no longer exported there".
                if not record.get("deleted"):
                    payload = json.loads(record.get("content") or "{}")
                    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
                        raise ValueError("Invalid memory fact record")
                    key = (payload.get("schema"), payload.get("owner"), payload.get("store_id"))
                    fact_groups.setdefault(key, []).append((rid, payload))
                continue
            if rid.startswith(SNAPSHOT_PREFIX):
                if not record.get("deleted"):
                    payload = json.loads(record.get("content") or "{}")
                    _store(payload.get("owner"))._facts.merge(payload)
                    applied += 1
                continue
            # Legacy records are read during upgrade but are never republished.
            # Legacy deletion was ambiguous (it could mean archive); it is not
            # allowed to override a v2 explicit save or forget barrier.
            if not rid.startswith("fact:") or record.get("deleted"):
                continue
            _, origin, local_id = rid.split(":", 2)
            store = _store()
            doc = store._facts.snapshot()
            if origin == ctx.device_id:
                origin = doc["store_id"]
            item = json.loads(record.get("content") or "{}")
            item.update(id=local_id, origin=origin, protected=False, revision=1)
            item.setdefault("provenance", "session")
            import time
            item.setdefault("expires_at", float(item.get("updated_at", 0)) + 90 * 86400 or time.time())
            store._facts.merge({"schema": 2, "owner": "local", "store_id": origin,
                                "entries": [item], "tombstones": []})
            applied += 1
        except (ValueError, KeyError, TypeError, OSError) as exc:
            _rejected(rid, exc)
    for (_schema, owner, _store_id), group in fact_groups.items():
        applied += _merge_fact_group(owner, group)
    return applied


def _merge_fact_group(owner, group) -> int:
    """Merge one store's fact and barrier records in one store write.

    A first sync can bring hundreds of records; one combined merge rewrites
    the store file once. If it is refused, merge record by record so one bad
    record does not hold back the others.
    """
    try:
        facts = _store(owner)._facts
    except (ValueError, OSError) as exc:
        _rejected(group[0][0], exc)
        return 0
    if len(group) > 1:
        first = group[0][1]
        combined = {"schema": first.get("schema"), "owner": owner, "store_id": first.get("store_id"),
                    "entries": [], "tombstones": []}
        bloom = None
        try:
            for _rid, payload in group:
                combined["entries"] += payload["entries"]
                combined["tombstones"] += payload.get("tombstones") or []
                if payload.get("forgotten"):
                    bits = bytes.fromhex(payload["forgotten"])
                    bloom = bits if bloom is None else bytes(x | y for x, y in zip(bloom, bits, strict=True))
            if bloom is not None:
                combined["forgotten"] = bloom.hex()
            facts.merge(combined)
            return len(group)
        except (ValueError, KeyError, TypeError, OSError):
            log.warning("Combined memory merge refused; merging %d records one by one", len(group), exc_info=True)
    applied = 0
    for rid, payload in group:
        try:
            facts.merge(payload)
            applied += 1
        except (ValueError, KeyError, TypeError, OSError) as exc:
            _rejected(rid, exc)
    return applied

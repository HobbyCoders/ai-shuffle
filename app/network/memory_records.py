"""Replicate bounded memory snapshots and separately managed procedural skills.

A missing hot fact is not a deletion. Fact revisions, expiry, archive and
forget barriers travel together; all imports use the normal transaction API.
Code-mode Python skills live under ``skills_root()``; prose skill folders
(``~/.fluid/skills/<name>/SKILL.md``) ride the same collection through
:mod:`app.network.prose_skills`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from app.db.base import get_db
from app.network import engine, prose_skills

log = logging.getLogger(__name__)
_SKILL_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}$")
MAX_SKILL_BYTES = 65536


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


def refresh_from_files(*, ctx=None) -> None:
    ctx = ctx or engine.context()
    if not ctx:
        return
    with prose_skills.LOCK:
        manifest = prose_skills.load_manifest()
        if _refresh(ctx, manifest):
            prose_skills.save_manifest(manifest)


def _refresh(ctx, manifest) -> bool:
    root = _root()
    incoming = {}
    for store in (_stores() if root.is_dir() else ()):
        payload = store._facts.export()
        # Usage counters affect local ranking, not fact truth or sync revisions.
        for entry in payload["entries"]:
            entry.pop("uses", None)
            entry.pop("last_recalled_at", None)
        content = json.dumps(payload, sort_keys=True)
        rid = f"memory-v2:{ctx.device_id}:{_hash(store._facts.owner)}"
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
        # Fact snapshots are not deleted for absence, archive or expiry.
        for rid, old in known.items():
            if rid.startswith("skill:") and rid not in incoming and not old.get("deleted") and root.is_dir():
                cur.execute("UPDATE network_memory_records SET deleted=TRUE, updated_at=NOW() WHERE id=%s", (rid,))
        changed = prose_skills.stage_publish(cur, known, manifest)
    return changed


def publish(*, ctx=None) -> int:
    ctx = ctx or engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return 0
    refresh_from_files(ctx=ctx)
    try:
        from app.network import authored_plugins
        with get_db() as conn:
            authored_plugins.refresh_from_files(conn.cursor())
    except Exception:  # noqa: BLE001 - drafts must not stop facts and skills
        log.warning("Publishing authored plugin drafts failed", exc_info=True)
    return engine.scan_collection("memory", ctx=ctx)


def apply_to_files(ids: list[str], *, ctx=None) -> int:
    ctx = ctx or engine.context()
    if not ctx or not ids:
        return 0
    with prose_skills.LOCK:
        manifest = prose_skills.load_manifest()
        before = json.dumps(manifest, sort_keys=True)
        applied = _apply(ids, ctx, manifest)
        if json.dumps(manifest, sort_keys=True) != before:
            prose_skills.save_manifest(manifest)
    return applied


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
            if rid.startswith("memory-v2:"):
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
        except (ValueError, KeyError, TypeError, OSError):
            log.exception("Memory replica rejected: %s", rid)
    return applied

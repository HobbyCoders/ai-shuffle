"""Continual memory and skills as synced records.

The fluid store under ``skills_root()`` keeps distilled facts in
``memories.json`` (entries with an id, text, scope, provenance) and saved
skills as ``<name>.py`` files. Both are shared across the network:

- facts: one record per entry, id ``fact:<device>:<entry id>`` so two
  workers' synthesizers never collide; a remote fact lands in the local
  ``memories.json`` with the same namespaced id, so it is rendered into the
  prompt on every worker (milestone 5: "a fact learned on one worker is in
  the prompt on another")
- skills: one record per file, id ``skill:<name>``; last writer wins

The journal (``journal.jsonl``) stays local: it is this worker's episodic
record and its synthesizer's input; syncing it would make every worker
consolidate the same episodes into duplicate facts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from app.db.base import get_db
from app.network import engine

logger = logging.getLogger(__name__)

_SKILL_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}$")
MAX_SKILL_BYTES = 65536


def _root() -> Optional[Path]:
    try:
        from app.core.fluid_memory import skills_root
        return skills_root()
    except Exception:  # noqa: BLE001
        return None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)


def _load_memories(root: Path) -> dict:
    path = root / "memories.json"
    try:
        doc = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        doc = {"next_id": 1, "entries": []}
    doc.setdefault("next_id", 1)
    if not isinstance(doc.get("entries"), list):
        doc["entries"] = []
    return doc


# ---------------------------------------------------------------------------
# Files -> records (publishing)
# ---------------------------------------------------------------------------

def refresh_from_files(*, ctx: Optional[engine.NetworkContext] = None) -> int:
    """Mirror this device's own facts and every skill file into the records table."""
    ctx = ctx or engine.context()
    root = _root()
    if ctx is None or root is None or not root.exists():
        return 0
    me = ctx.device_id
    rows: List[Tuple[str, str, str, str]] = []
    for entry in _load_memories(root)["entries"]:
        if not isinstance(entry, dict) or not entry.get("id") or not str(entry.get("text", "")).strip():
            continue
        entry_id = str(entry["id"])
        if ":" in entry_id and not entry_id.startswith(me + ":"):
            continue  # a fact another device published; it publishes it
        local_id = entry_id.split(":", 1)[1] if ":" in entry_id else entry_id
        content = json.dumps({k: entry.get(k) for k in ("text", "scope", "provenance", "created_at", "updated_at")},
                             sort_keys=True, ensure_ascii=False)
        rows.append((f"fact:{me}:{local_id}", "fact", str(entry.get("scope") or "global"), content))
    for path in sorted(root.glob("*.py")):
        if not _SKILL_NAME.match(path.stem):
            continue
        try:
            source = path.read_text("utf-8")
        except OSError:
            continue
        if len(source.encode("utf-8", "replace")) > MAX_SKILL_BYTES:
            continue
        rows.append((f"skill:{path.stem}", "skill", "", source))
    changed = 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, content_hash, deleted FROM network_memory_records")
        known = {r["id"]: (r["content_hash"], bool(r["deleted"])) for r in cursor.fetchall()}
        seen = set()
        for record_id, kind, scope, content in rows:
            seen.add(record_id)
            digest = _hash(content)
            if known.get(record_id) == (digest, False):
                continue
            cursor.execute(
                """INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted, updated_at)
                   VALUES (%s, %s, %s, %s, %s, FALSE, NOW())
                   ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind, scope = EXCLUDED.scope,
                       content = EXCLUDED.content, content_hash = EXCLUDED.content_hash, deleted = FALSE,
                       updated_at = NOW()""",
                (record_id, kind, scope, content, digest))
            changed += 1
        # Our own facts and any skill that vanished from disk become tombstones.
        for record_id, (digest, deleted) in known.items():
            if record_id in seen or deleted:
                continue
            if record_id.startswith("fact:") and not record_id.startswith(f"fact:{me}:"):
                continue
            cursor.execute("UPDATE network_memory_records SET deleted = TRUE, content = '', content_hash = '', updated_at = NOW() WHERE id = %s",
                           (record_id,))
            changed += 1
    return changed


def publish(*, ctx: Optional[engine.NetworkContext] = None) -> int:
    ctx = ctx or engine.context()
    if ctx is None or ctx.bootstrap_state != engine.BOOTSTRAP_READY:
        return 0
    refresh_from_files(ctx=ctx)
    return engine.scan_collection("memory", ctx=ctx)


# ---------------------------------------------------------------------------
# Records -> files (applying)
# ---------------------------------------------------------------------------

def apply_to_files(record_ids: Iterable[str]) -> int:
    """Write remote facts into memories.json and remote skills to disk."""
    root = _root()
    ctx = engine.context()
    if root is None or ctx is None:
        return 0
    ids = list(record_ids)
    if not ids:
        return 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_memory_records WHERE id IN (%s)" % ",".join(["%s"] * len(ids)), tuple(ids))
        records = [dict(r) for r in cursor.fetchall()]
    applied = 0
    facts = [r for r in records if r["kind"] == "fact"]
    if facts:
        root.mkdir(parents=True, exist_ok=True)
        doc = _load_memories(root)
        entries = doc["entries"]
        by_id = {str(e.get("id")): e for e in entries if isinstance(e, dict)}
        for record in facts:
            _, device_id, local_id = record["id"].split(":", 2)
            if device_id == ctx.device_id:
                continue
            namespaced = f"{device_id}:{local_id}"
            if bool(record["deleted"]):
                if namespaced in by_id:
                    entries[:] = [e for e in entries if not (isinstance(e, dict) and e.get("id") == namespaced)]
                    applied += 1
                continue
            try:
                payload = json.loads(record["content"] or "{}")
            except ValueError:
                continue
            text = " ".join(str(payload.get("text") or "").split())
            if not text:
                continue
            entry = by_id.get(namespaced)
            if entry is None:
                entry = {"id": namespaced, "uses": 0, "last_recalled_at": 0.0}
                entries.append(entry)
                by_id[namespaced] = entry
            entry.update({"text": text, "scope": payload.get("scope") or "global",
                          "provenance": payload.get("provenance") or "session",
                          "created_at": float(payload.get("created_at") or 0.0),
                          "updated_at": float(payload.get("updated_at") or 0.0)})
            applied += 1
        _atomic_write(root / "memories.json", json.dumps(doc, indent=2, ensure_ascii=False))
    for record in records:
        if record["kind"] != "skill":
            continue
        name = record["id"].split(":", 1)[1]
        if not _SKILL_NAME.match(name):
            continue
        path = root / f"{name}.py"
        if bool(record["deleted"]):
            try:
                path.unlink()
                applied += 1
            except FileNotFoundError:
                pass
            continue
        source = record["content"] or ""
        try:
            if path.exists() and path.read_text("utf-8") == source:
                continue
        except OSError:
            pass
        _atomic_write(path, source)
        applied += 1
    return applied

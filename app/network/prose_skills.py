"""Replicate user-level prose skill folders (``<name>/SKILL.md`` plus small text files).

Source of truth is :func:`app.core.fluid_memory.prose_skills_root`
(``~/.fluid/skills``), the directory fluid's skill discovery reads. Each folder
travels as one ``prose_skill`` record in the memory collection, id
``prose-skill:<name>``; the folder is the unit of last-writer-wins.

Only regular UTF-8 text files with safe names, at most ``MAX_DEPTH`` folders
deep, are synced. Binary or oversized extras are left out of the record and are
never touched on any worker. A folder whose SKILL.md or totals exceed the limits
is held: not published and not deleted, so a local edit past a limit cannot
erase the skill elsewhere.

A local manifest (``<data_dir>/fluid/prose_skills_sync.json``) remembers each
folder's state at its last publish or apply. It is what separates a local edit
from a remote change that has reached the database but not the disk yet:

- refresh publishes a folder only when the disk differs from the manifest, and
  tombstones only names the manifest knew, so a peer's record that is not yet
  materialized here is never deleted or reverted;
- apply skips a folder whose disk differs from the manifest (an unpublished
  local edit), which the next refresh publishes, and deletes only files the
  manifest or the current sync set lists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

log = logging.getLogger(__name__)

PREFIX = "prose-skill:"
KIND = "prose_skill"
SCHEMA = 1
MAX_FILE_BYTES = 65536
MAX_TOTAL_BYTES = 256 * 1024
MAX_FILES = 32
MAX_DEPTH = 3  # SKILL.md is depth 1; references/sub/x.md is depth 3
SKILL_FILE = "SKILL.md"
_SKIP_NAMES = frozenset({"__pycache__"})

# Refresh and apply both read-modify-write the manifest and the folders.
LOCK = threading.RLock()

# Held folder name -> the reason already logged, so each refresh does not warn again.
_held_logged: Dict[str, str] = {}


@dataclass(frozen=True)
class Snapshot:
    content: str
    digest: str
    files: Tuple[str, ...]


class Held(Exception):
    """The folder is a skill but exceeds a limit; leave every copy alone."""


def _name_ok(name: str) -> bool:
    from app.network.memory_records import _SKILL_NAME
    return isinstance(name, str) and bool(_SKILL_NAME.fullmatch(name)) and name not in _SKIP_NAMES


def _path_ok(rel: str) -> bool:
    parts = rel.split("/") if isinstance(rel, str) else []
    return 0 < len(parts) <= MAX_DEPTH and all(_name_ok(p) for p in parts)


def root() -> Path:
    from app.core.fluid_memory import prose_skills_root
    return prose_skills_root()


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _is_link(path: str) -> bool:
    return os.path.islink(path) or bool(getattr(os.path, "isjunction", lambda _p: False)(path))


def encode(files: Mapping[str, str]) -> str:
    return json.dumps({"schema": SCHEMA, "files": dict(files)}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def decode(content: str) -> Optional[Dict[str, str]]:
    """The record's files, or None when it breaks any limit a sender enforces."""
    try:
        doc = json.loads(content or "")
    except ValueError:
        return None
    files = doc.get("files") if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None
    if not isinstance(files, dict) or SKILL_FILE not in files or len(files) > MAX_FILES:
        return None
    total = 0
    for rel, text in files.items():
        if not _path_ok(rel) or not isinstance(text, str):
            return None
        size = len(text.encode())
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            return None
    return files


def snapshot(skill_dir: Path) -> Optional[Snapshot]:
    """The folder's synced files; None when it is not a skill. Raises Held."""
    if _is_link(str(skill_dir)) or not skill_dir.is_dir():
        return None
    entries = []

    def walk(directory: str, prefix: str, depth: int) -> None:
        with os.scandir(directory) as it:
            for entry in sorted(it, key=lambda e: e.name):
                if not _name_ok(entry.name) or _is_link(entry.path):
                    continue
                rel = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        walk(entry.path, rel + "/", depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    entries.append((rel, entry))

    walk(str(skill_dir), "", 1)
    if not any(rel == SKILL_FILE for rel, _ in entries):
        return None
    files: Dict[str, str] = {}
    total = 0
    for rel, entry in entries:
        oversized = entry.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES
        try:
            text = None if oversized else Path(entry.path).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            if rel == SKILL_FILE:
                raise Held("SKILL.md is over the size limit or not UTF-8")
            continue  # binary or large extras stay local
        total += len(text.encode())
        files[rel] = text
    if len(files) > MAX_FILES or total > MAX_TOTAL_BYTES:
        raise Held("too many files or bytes")
    content = encode(files)
    return Snapshot(content, _hash(content), tuple(sorted(files)))


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _manifest_path() -> Path:
    from app.core.config import settings
    return settings.effective_data_dir / "fluid" / "prose_skills_sync.json"


def load_manifest() -> Dict[str, Dict[str, Any]]:
    try:
        doc = json.loads(_manifest_path().read_text(encoding="utf-8"))
        skills = doc.get("skills") if doc.get("schema") == SCHEMA else None
        return {k: v for k, v in (skills or {}).items()
                if _name_ok(k) and isinstance(v, dict) and isinstance(v.get("hash"), str)}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError):
        log.warning("Prose skill sync manifest unreadable; starting over")
        return {}


def save_manifest(manifest: Mapping[str, Mapping[str, Any]]) -> None:
    from fluid.codemode.skills import atomic_write_text
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"schema": SCHEMA, "skills": dict(manifest)}, sort_keys=True))


# ---------------------------------------------------------------------------
# Publish side
# ---------------------------------------------------------------------------

def _note_held(name: str, reason: str) -> bool:
    """Record why ``name`` is held; True only when that reason is new for it."""
    with LOCK:
        if _held_logged.get(name) == reason:
            return False
        _held_logged[name] = reason
        return True


def stage_publish(cur, known: Mapping[str, Mapping[str, Any]], manifest: Dict[str, Dict[str, Any]]) -> bool:
    """Write changed folders as rows on ``cur``; update ``manifest`` in place.

    Returns whether the manifest changed. The caller saves it only after the
    transaction commits, so a failed commit is retried from the disk next time.
    """
    base = root()
    if not base.is_dir():
        return False  # a missing root is not a mass deletion
    present, held = {}, set()
    try:
        children = sorted(base.iterdir())
    except OSError:
        log.warning("Prose skills root unreadable", exc_info=True)
        return False
    for child in children:
        if not _name_ok(child.name):
            continue
        try:
            snap = snapshot(child)
        except Held as exc:
            held.add(child.name)
            if _note_held(child.name, f"held: {exc}"):
                log.warning("Prose skill %s not synced: %s", child.name, exc)
            continue
        except OSError as exc:
            held.add(child.name)
            if _note_held(child.name, f"unreadable: {exc}"):
                log.warning("Prose skill %s unreadable", child.name, exc_info=True)
            continue
        if snap is not None:
            present[child.name] = snap
    with LOCK:
        for name in [n for n in _held_logged if n not in held]:
            del _held_logged[name]
            if name in present:
                log.info("Prose skill %s now synced", name)
    changed = False
    for name, snap in present.items():
        rid = PREFIX + name
        row = known.get(rid)
        synced = manifest.get(name, {}).get("hash") == snap.digest
        if synced and row is not None:
            continue  # no local edit; the row may hold a newer peer version not applied yet
        if row is None or row.get("content_hash") != snap.digest or row.get("deleted"):
            _upsert(cur, rid, snap.content, snap.digest)
        manifest[name] = {"hash": snap.digest, "files": list(snap.files)}
        changed = True
    for name in [n for n in manifest if n not in present and n not in held]:
        row = known.get(PREFIX + name)
        if row is not None and not row.get("deleted"):
            cur.execute("UPDATE network_memory_records SET deleted=TRUE, updated_at=NOW() WHERE id=%s",
                        (PREFIX + name,))
        manifest.pop(name)
        changed = True
    return changed


def _upsert(cur, rid: str, content: str, digest: str) -> None:
    cur.execute(
        "INSERT INTO network_memory_records (id, kind, scope, content, content_hash, deleted) "
        "VALUES (%s, %s, 'global', %s, %s, FALSE) ON CONFLICT (id) DO UPDATE SET "
        "kind=EXCLUDED.kind, scope=EXCLUDED.scope, content=EXCLUDED.content, "
        "content_hash=EXCLUDED.content_hash, deleted=FALSE, updated_at=NOW()",
        (rid, KIND, content, digest),
    )


# ---------------------------------------------------------------------------
# Apply side
# ---------------------------------------------------------------------------

def _atomic_write_bytes(path: Path, data: bytes) -> None:
    # Bytes, not text: a newline translation would change the hash and make
    # the receiver republish what it was sent.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _safe_target(skill_dir: Path, rel: str) -> Path:
    """``skill_dir/rel`` with no link anywhere between the root and the file."""
    path = skill_dir
    for part in rel.split("/"):
        if _is_link(str(path)):
            raise OSError(f"Refusing to write through a link: {path}")
        path = path / part
    if _is_link(str(path)):
        raise OSError(f"Refusing to replace a link: {path}")
    return path


def _remove(skill_dir: Path, rels) -> None:
    """Delete synced files, then any folder between them and ``skill_dir`` left empty."""
    folders = set()
    for rel in rels:
        if not _path_ok(rel):
            continue
        target = _safe_target(skill_dir, rel)
        if target.is_file():
            target.unlink()
        folders.update(target.parents[:len(rel.split("/"))])  # up to and including skill_dir
    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass  # not empty: holds local-only files


def apply_record(record: Mapping[str, Any], manifest: Dict[str, Dict[str, Any]]) -> bool:
    """Materialize one record; returns whether it was applied. Raises OSError."""
    name = str(record["id"]).removeprefix(PREFIX)
    if not _name_ok(name):
        return False
    base = root()
    skill_dir = base / name
    if _is_link(str(skill_dir)):
        log.warning("Prose skill %s is a link; not replaced", name)
        return False
    try:
        current = snapshot(skill_dir)
    except Held:
        return False  # local copy past a limit: never clobber it
    known = manifest.get(name)
    if current is not None and current.digest != record.get("content_hash") \
            and (known is None or known.get("hash") != current.digest):
        return False  # unpublished local edit; the next refresh publishes it and LWW decides
    owned: set = set(known.get("files", ())) if known else set()
    if current is not None:
        owned |= set(current.files)
    if record.get("deleted"):
        _remove(skill_dir, owned)
        manifest.pop(name, None)
        return True
    files = decode(record.get("content") or "")
    if files is None:
        log.warning("Prose skill record %s rejected: outside the sync limits", name)
        return False
    if current is not None and current.digest == record.get("content_hash"):
        manifest[name] = {"hash": current.digest, "files": list(current.files)}
        return True
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel in sorted(files):
        target = _safe_target(skill_dir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = files[rel].encode()
        if target.is_file() and target.read_bytes() == data:
            continue
        _atomic_write_bytes(target, data)
    _remove(skill_dir, owned - set(files))
    after = snapshot(skill_dir)
    if after is not None:
        manifest[name] = {"hash": after.digest, "files": list(after.files)}
    return True

"""Cross-install agent-memory sync.

Mirrors the Claude Code CLI's per-project memory directory
(``<claude_projects>/<slug>/memory/`` — ``MEMORY.md`` plus per-fact ``*.md``
files) to and from the shared database so installs that share a git remote
share one logical memory.

Two operations drive it from the session lifecycle (see app/core/query_engine.py):

* **reconcile** — runs before a fresh CLI subprocess spawns. Brings the local
  memory dir up to date with the DB (and pushes any local-only files up), so
  the agent reads the latest shared memory.
* **push** — runs after each turn, once the CLI has flushed memory to disk.
  Upserts changed/new local files and tombstones files deleted locally.

Conflict resolution is last-write-wins by timestamp (DB ``updated_at`` vs local
mtime). This assumes cluster clocks are roughly NTP-synced; simultaneous edits
to the *same* memory file from overlapping sessions on different installs are
rare in practice (memory writes are sparse and the nightly consolidation is the
main rewriter). Everything here is best-effort and must never break a turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.core.config import settings
from app.core.jsonl_parser import get_project_dir_name
from app.db import agent_memory as memdb
from app.db import database

logger = logging.getLogger(__name__)

# memory_keys reconciled at least once in THIS process. push() only tombstones
# (propagates deletes) for a key once reconcile has established the local dir as
# authoritative — otherwise a stale/empty local dir could wrongly wipe the DB.
_reconciled_keys: set = set()

# (content, sha256, mtime) for each local memory file, keyed by relative path.
_LocalState = Dict[str, Tuple[str, str, datetime]]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_dt(value: object) -> datetime:
    """Coerce a DB timestamp (datetime or ISO string) to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        try:
            dt = datetime.fromisoformat(str(value))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _safe_target(memory_dir: Path, relative_path: str) -> Optional[Path]:
    """Resolve relative_path under memory_dir, refusing any path escape.

    Content here crosses installs via the shared DB, so a hostile/buggy
    relative_path (e.g. ``../../x``) must never write outside the memory dir.
    """
    base = memory_dir.resolve()
    target = (memory_dir / relative_path).resolve()
    if target != base and base not in target.parents:
        logger.warning("memory_sync: refusing out-of-tree path %r", relative_path)
        return None
    return target


def memory_key_for_project(project: dict) -> Optional[str]:
    """Cross-install-stable key for a project: link_group_id, else project_id.

    See app/core/project_links.py — link_group_id is derived from the git
    remote and identical across installs that clone the same repo.
    """
    if not project:
        return None
    return project.get("link_group_id") or project.get("id")


def _resolve_memory_key(project_id: str) -> Optional[str]:
    try:
        project = database.get_project(project_id)
    except Exception:  # noqa: BLE001 — never break the caller
        logger.debug("memory_sync: get_project failed for %s", project_id, exc_info=True)
        return None
    return memory_key_for_project(project)


def memory_dir_for_working_dir(working_dir: str) -> Optional[Path]:
    """`<claude_projects>/<slug>/memory/` for a session's working directory."""
    try:
        return settings.get_claude_projects_dir / get_project_dir_name(working_dir) / "memory"
    except Exception:  # noqa: BLE001
        logger.debug("memory_sync: could not resolve memory dir for %r", working_dir, exc_info=True)
        return None


def _scan_local(memory_dir: Path) -> _LocalState:
    state: _LocalState = {}
    if not memory_dir.exists():
        return state
    base = memory_dir.resolve()
    for path in memory_dir.rglob("*.md"):
        if not path.is_file():
            continue
        # Refuse entries that resolve outside the memory tree — a planted .md
        # symlink (leaf, or via a symlinked parent dir) pointing at a secret
        # would otherwise be slurped into the shared DB and replicated to peers.
        # rglob/is_file follow symlinks, so this containment check (not a leaf
        # is_symlink test) is what actually closes the read-side hole.
        real = path.resolve()
        if real != base and base not in real.parents:
            logger.warning("memory_sync: skipping out-of-tree memory file %s", path)
            continue
        try:
            content = path.read_text(encoding="utf-8")
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(memory_dir).as_posix()
        state[rel] = (content, _sha256(content), mtime)
    return state


def _write_local(memory_dir: Path, relative_path: str, content: str) -> None:
    target = _safe_target(memory_dir, relative_path)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _remove_local(memory_dir: Path, relative_path: str) -> None:
    target = _safe_target(memory_dir, relative_path)
    if target is None:
        return
    try:
        target.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Core reconcile / push (operate on a resolved memory_key + dir; testable)
# ---------------------------------------------------------------------------

def reconcile(memory_key: str, memory_dir: Path, instance_id: Optional[str]) -> None:
    """Bring the local memory dir into agreement with the DB (last-write-wins)."""
    db_rows = {r["relative_path"]: r for r in memdb.get_memory_files(memory_key)}
    local = _scan_local(memory_dir)

    for rel, row in db_rows.items():
        db_dt = _as_dt(row.get("updated_at"))
        if row.get("deleted"):
            # Remote deletion: drop the local copy unless local is newer (revived).
            if rel in local and db_dt >= local[rel][2]:
                _remove_local(memory_dir, rel)
            continue

        db_content = row.get("content") or ""
        db_hash = row.get("content_hash") or _sha256(db_content)
        if rel not in local:
            _write_local(memory_dir, rel, db_content)
        else:
            _, local_hash, local_mtime = local[rel]
            if local_hash != db_hash and db_dt >= local_mtime:
                _write_local(memory_dir, rel, db_content)
            # local newer (or equal content) → keep; push() will sync it up

    # Files only on local disk are left as-is; push() uploads them.
    _reconciled_keys.add(memory_key)


def push(memory_key: str, memory_dir: Path, instance_id: Optional[str],
         allow_deletes: bool) -> None:
    """Upload local changes to the DB; tombstone local deletions if allowed."""
    db_rows = {r["relative_path"]: r for r in memdb.get_memory_files(memory_key)}
    local = _scan_local(memory_dir)

    for rel, (content, local_hash, local_mtime) in local.items():
        row = db_rows.get(rel)
        if row is None:
            memdb.upsert_memory_file(memory_key, rel, content, local_hash, instance_id)
            continue
        if row.get("deleted") or row.get("content_hash") != local_hash:
            # Don't clobber a newer remote write (last-write-wins). A tombstone
            # is always overwritten by a present local file (revive).
            if row.get("deleted") or local_mtime >= _as_dt(row.get("updated_at")):
                memdb.upsert_memory_file(memory_key, rel, content, local_hash, instance_id)

    if allow_deletes:
        for rel, row in db_rows.items():
            if not row.get("deleted") and rel not in local:
                memdb.tombstone_memory_file(memory_key, rel, instance_id)


# ---------------------------------------------------------------------------
# Public sync entry points (resolve project/dir, gate, then reconcile/push).
# Synchronous — call via asyncio.to_thread from the async lifecycle.
# ---------------------------------------------------------------------------

def reconcile_memory_sync(project_id: Optional[str], working_dir: Optional[str]) -> None:
    if not settings.agent_memory_sync or not project_id or not working_dir:
        return
    memory_key = _resolve_memory_key(project_id)
    memory_dir = memory_dir_for_working_dir(working_dir) if memory_key else None
    if not memory_key or memory_dir is None:
        return
    reconcile(memory_key, memory_dir, settings.effective_instance_id)


def push_memory_sync(project_id: Optional[str], working_dir: Optional[str]) -> None:
    if not settings.agent_memory_sync or not project_id or not working_dir:
        return
    memory_key = _resolve_memory_key(project_id)
    memory_dir = memory_dir_for_working_dir(working_dir) if memory_key else None
    if not memory_key or memory_dir is None:
        return
    allow_deletes = memory_key in _reconciled_keys and memory_dir.exists()
    push(memory_key, memory_dir, settings.effective_instance_id, allow_deletes)


# ---------------------------------------------------------------------------
# Async wrappers for the event loop — offload sync DB + file I/O to a thread,
# never raise into the caller (a sync failure must not break a chat turn).
# ---------------------------------------------------------------------------

async def reconcile_memory(project_id: Optional[str], working_dir: Optional[str]) -> None:
    if not settings.agent_memory_sync:
        return
    try:
        await asyncio.to_thread(reconcile_memory_sync, project_id, working_dir)
    except Exception:  # noqa: BLE001
        logger.debug("memory_sync: reconcile failed", exc_info=True)


async def push_memory(project_id: Optional[str], working_dir: Optional[str]) -> None:
    if not settings.agent_memory_sync:
        return
    try:
        await asyncio.to_thread(push_memory_sync, project_id, working_dir)
    except Exception:  # noqa: BLE001
        logger.debug("memory_sync: push failed", exc_info=True)

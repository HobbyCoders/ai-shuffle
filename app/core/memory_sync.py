"""Cross-install agent-memory sync — NATIVE sessions only.

Mirrors a per-project memory directory (``MEMORY.md`` plus per-fact ``*.md``
files) to and from the shared database so installs that share a git remote
share one logical memory.

Only the NATIVE leg participates. A fluid NativeLoop session has no CLI behind
it, so the app owns ``<fluid_projects>/<slug>/memory/`` (~/.fluid/projects by
default). That fluid dir is the one and only synced mirror. No prompt section
points at it (see docs/MEMORY_ARCHITECTURE.md), so the sync serves pre-existing
content and the explicit per-project import only; durable facts travel through
the continual learner instead.

The DELEGATED leg does not sync at all. ``<claude_projects>/<slug>/memory/``
belongs to the claude CLI: the CLI writes it, the CLI reads it, and it stays
local to this machine. Nothing pushes it to the shared DB and nothing writes
into it. That is why ``reconcile_memory_sync``/``push_memory_sync`` return
immediately unless ``leg == "native"`` — and why the leg decision degrading to
"delegated" whenever it cannot be made (see ``memory_leg_for_options``) is
safe: an undecidable session simply does not sync.

``memory_dir_for_working_dir`` stays leg-aware regardless, because locating the
claude dir is still needed — not for sync, but for the explicit per-project
import (see app/core/memory_import_service.py). Reconcile never copies claude
memory into the fluid dir.

Two operations drive the native leg from the session lifecycle (see
app/core/query_engine.py and app/core/query_background.py):

* **reconcile** — runs before a session starts. Brings the legacy local memory
  directory up to date with the DB and pushes local-only files. It does not
  populate the continual learner's per-turn `# Memory` snapshot.
* **push** — runs after each turn, once memory has been flushed to disk.
  Upserts changed/new local files and tombstones files *genuinely* deleted
  locally — those that were in this process's reconcile baseline and have since
  disappeared. A DB row that was never in our baseline (a topic file a peer
  install just authored, or one that only lives in a peer's memory dir) is NOT a
  local delete and is never tombstoned; reconcile pulls it down instead.

Conflict resolution is last-write-wins by timestamp (DB ``updated_at`` vs local
mtime). This assumes cluster clocks are roughly NTP-synced; simultaneous edits
to the *same* memory file from overlapping sessions on different installs are
rare in practice — memory writes are sparse. Everything here is best-effort and
must never break a turn.

Deletes are propagated non-destructively: like the notes sync worker, an install
never tombstones a shared file merely because its own local dir lacks it — only
a file it demonstrably HELD and then dropped. This is what stops two installs
that share a memory_key (git link group) but keep separate on-disk memory dirs
(different project slugs / worktrees) from mirror-deleting each other's files.
The gate and the baseline are therefore keyed per dir, not per memory_key.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.core.config import settings
from app.core.jsonl_parser import get_project_dir_name
from app.core.project_utils import get_project_working_dir
from app.db import agent_memory as memdb
from app.db import database

logger = logging.getLogger(__name__)

# (memory_key, memory_dir) pairs reconciled at least once in THIS process. push()
# only tombstones (propagates deletes) for a pair once reconcile has established
# THAT dir as authoritative — otherwise a stale/empty local dir could wrongly
# wipe the DB. Keyed by dir, not by key alone: installs (and worktrees) that
# share a memory_key keep separate mirrors, so a reconcile of one dir must
# never authorize deletes against another's.
_reconciled_keys: set = set()

# Per-(memory_key, memory_dir) baseline: the set of relative_paths this process has confirmed
# were materialized locally (pulled from the DB during reconcile, or uploaded by
# us during push). This is the three-way-merge anchor that lets push tell a
# genuine LOCAL DELETE (a path that WAS in our baseline and is now gone) apart
# from a path that merely never existed here — one a peer install just added to
# the shared DB after our last reconcile, or that lives only in a peer's memory
# dir. Without this anchor, push tombstoned every DB row absent from the local
# scan, so concurrent installs/worktrees (same memory_key, different memory dir)
# mirror-deleted each other's freshly-authored topic files.
_local_baseline: Dict[Tuple[str, str], set] = {}


def _state_key(memory_key: str, memory_dir: Path) -> Tuple[str, str]:
    """Gate/baseline key: one entry per (memory_key, local mirror dir)."""
    return (memory_key, str(memory_dir))

# (content, sha256, mtime) for each local memory file, keyed by relative path.
_LocalState = Dict[str, Tuple[str, str, datetime]]


def reset_state() -> None:
    """Clear all in-memory sync state (reconcile gate + baselines). For tests."""
    _reconciled_keys.clear()
    _local_baseline.clear()


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


# A session's cwd may be a git worktree at ``<repo>/.fluid/worktrees/<name>``
# or a legacy ``<repo>/.claude/worktrees/<name>`` (see worktree_manager.py).
# The Claude CLI anchors a project's NATIVE memory dir to the MAIN checkout's
# slug regardless of the worktree cwd, so slugging the worktree path targets a
# per-worktree mirror the CLI never populates — sync then silently no-ops.
# Reduce such a cwd to its repo root before slugging.
_WORKTREE_MARKER_RE = re.compile(
    r"[\\/](?:\.fluid|\.claude)[\\/]worktrees[\\/]"
)


def _repo_root_for_working_dir(working_dir: str) -> str:
    """Strip a trailing ``.fluid/worktrees/<name>`` segment to the repo root."""
    match = _WORKTREE_MARKER_RE.search(working_dir)
    return working_dir[: match.start()] if match else working_dir


def memory_dir_for_working_dir(
    working_dir: str, leg: str = "delegated"
) -> Optional[Path]:
    """`<projects_root>/<slug>/memory/` for a session's working directory.

    The root is the LEG's: ``~/.claude/projects`` for the
    delegated claude CLI, ``~/.fluid/projects`` for a native fluid leg. The slug
    is the same path-based normalization either way, so the two mirrors line up
    project-for-project.

    Worktree cwds are folded back to the main checkout first (see above) so the
    slug matches the dir the CLI actually writes memory into.
    """
    try:
        root = (
            settings.get_fluid_projects_dir
            if leg == "native"
            else settings.get_claude_projects_dir
        )
        anchor = _repo_root_for_working_dir(working_dir)
        return root / get_project_dir_name(anchor) / "memory"
    except Exception:  # noqa: BLE001
        logger.debug("memory_sync: could not resolve memory dir for %r", working_dir, exc_info=True)
        return None


def memory_leg_for_options(options: object) -> str:
    """"native" | "delegated" for a built ``AgentOptions`` object.

    Defers to the harness adapter's single leg-decision table (see
    ``provider_kind_for_sdk_options`` / ``_decide_provider_kind`` in
    app/core/provider/harness_adapter.py) rather than re-deriving it here — a
    second copy would drift the moment the provider table changes. Imported
    lazily: memory_sync is on the import path of modules that must not drag the
    fluid harness in.

    Anything unknowable (no options, an adapter import failure, a kind this
    module has never heard of) degrades to "delegated", i.e. today's behaviour.
    """
    if options is None:
        return "delegated"
    try:
        from app.core.provider.harness_adapter import provider_kind_for_sdk_options

        kind = provider_kind_for_sdk_options(options)
    except Exception:  # noqa: BLE001 — never break a turn over a dir choice
        logger.debug("memory_sync: could not decide leg; assuming delegated", exc_info=True)
        return "delegated"
    return "delegated" if kind in (None, "delegated") else "native"


def _resolve_target(
    project_id: str, working_dir: str, leg: str = "delegated"
) -> Tuple[Optional[str], Optional[Path]]:
    """Resolve a session's (memory_key, memory_dir), anchored on the project.

    The memory dir is slugged from the PROJECT record's root path (its main
    checkout), not the raw session cwd: a worktree session runs under
    ``<repo>/.fluid/worktrees/<name>`` but the CLI writes native memory to the
    main checkout's slug dir. Falling back to ``working_dir`` (itself folded to
    a repo root by ``memory_dir_for_working_dir``) preserves behaviour when the
    project lookup yields nothing.

    ``leg`` picks the projects root; the memory_key is the same
    either way, which is what makes the two mirrors one shared memory.
    """
    try:
        project = database.get_project(project_id)
    except Exception:  # noqa: BLE001 — never break the caller
        logger.debug("memory_sync: get_project failed for %s", project_id, exc_info=True)
        project = None
    memory_key = memory_key_for_project(project)
    if not memory_key:
        return None, None
    anchor = working_dir
    try:
        resolved = get_project_working_dir(project)
        if resolved:
            anchor = resolved
    except Exception:  # noqa: BLE001 — fall back to the session cwd
        logger.debug("memory_sync: could not resolve project working dir", exc_info=True)
    return memory_key, memory_dir_for_working_dir(anchor, leg)


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

def reconcile(memory_key: str, memory_dir: Path, instance_id: Optional[str],
              leg: str = "delegated") -> None:
    """Bring the local memory dir into agreement with the DB (last-write-wins)."""
    db_rows = {r["relative_path"]: r for r in memdb.get_memory_files(memory_key)}
    local = _scan_local(memory_dir)

    # Track which paths end up materialized locally so push can distinguish a
    # genuine local delete from a never-here path. Seed with local-only files
    # (present but not in the DB) — they belong to us and push will upload them.
    present = set(local.keys())

    for rel, row in db_rows.items():
        db_dt = _as_dt(row.get("updated_at"))
        if row.get("deleted"):
            # Remote deletion: drop the local copy unless local is newer (revived).
            if rel in local and db_dt >= local[rel][2]:
                _remove_local(memory_dir, rel)
                present.discard(rel)
            continue

        db_content = row.get("content") or ""
        db_hash = row.get("content_hash") or _sha256(db_content)
        if rel not in local:
            _write_local(memory_dir, rel, db_content)
            present.add(rel)
        else:
            _, local_hash, local_mtime = local[rel]
            if local_hash != db_hash and db_dt >= local_mtime:
                _write_local(memory_dir, rel, db_content)
            # local newer (or equal content) → keep; push() will sync it up

    # Files only on local disk are left as-is; push() uploads them.
    _reconciled_keys.add(_state_key(memory_key, memory_dir))
    _local_baseline[_state_key(memory_key, memory_dir)] = present
    # One visible line so a silently-mis-resolved dir (e.g. a worktree slug the
    # CLI never writes) can't hide as a clean no-op ever again.
    logger.info(
        "memory_sync: reconcile key=%s leg=%s dir=%s db_files=%d local_files=%d",
        memory_key, leg, memory_dir, len(db_rows), len(local),
    )


def push(memory_key: str, memory_dir: Path, instance_id: Optional[str],
         allow_deletes: bool, leg: str = "delegated") -> None:
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
        # Only tombstone a GENUINE local delete: a path we had in our baseline
        # (materialized here by a prior reconcile/push) that has since vanished
        # from disk. A live DB row we never held is a peer's file — leave it be,
        # reconcile will pull it down. This is the guard that stops installs
        # sharing a memory_key but not a memory dir from wiping each other.
        baseline = _local_baseline.get(_state_key(memory_key, memory_dir), set())
        for rel in baseline - set(local.keys()):
            row = db_rows.get(rel)
            if row is not None and not row.get("deleted"):
                memdb.tombstone_memory_file(memory_key, rel, instance_id)

    # Refresh the baseline to the current on-disk set: files we just uploaded are
    # now ours to track; files we tombstoned are gone. A peer file still absent
    # here stays out of the baseline until a reconcile materializes it.
    _local_baseline[_state_key(memory_key, memory_dir)] = set(local.keys())
    logger.info(
        "memory_sync: push key=%s leg=%s dir=%s db_files=%d local_files=%d deletes=%s",
        memory_key, leg, memory_dir, len(db_rows), len(local), allow_deletes,
    )


# ---------------------------------------------------------------------------
# Public sync entry points (resolve project/dir, gate, then reconcile/push).
# Synchronous — call via asyncio.to_thread from the async lifecycle.
# ---------------------------------------------------------------------------

def reconcile_memory_sync(project_id: Optional[str], working_dir: Optional[str],
                          leg: str = "delegated") -> None:
    # Native only. The delegated claude CLI keeps its own memory to itself, and
    # every undecidable leg degrades to "delegated" — so this one check is also
    # what makes an unrecognized session fail closed (no sync) instead of
    # writing into a dir the app does not own.
    if leg != "native":
        return
    if not settings.agent_memory_sync or not project_id or not working_dir:
        return
    memory_key, memory_dir = _resolve_target(project_id, working_dir, leg)
    if not memory_key or memory_dir is None:
        return
    reconcile(memory_key, memory_dir, settings.effective_instance_id, leg)


def push_memory_sync(project_id: Optional[str], working_dir: Optional[str],
                     leg: str = "delegated") -> None:
    # Native only — see reconcile_memory_sync.
    if leg != "native":
        return
    if not settings.agent_memory_sync or not project_id or not working_dir:
        return
    memory_key, memory_dir = _resolve_target(project_id, working_dir, leg)
    if not memory_key or memory_dir is None:
        return
    allow_deletes = (
        _state_key(memory_key, memory_dir) in _reconciled_keys and memory_dir.exists()
    )
    push(memory_key, memory_dir, settings.effective_instance_id, allow_deletes, leg)


# ---------------------------------------------------------------------------
# Async wrappers for the event loop — offload sync DB + file I/O to a thread,
# never raise into the caller (a sync failure must not break a chat turn).
# ---------------------------------------------------------------------------

async def reconcile_memory(project_id: Optional[str], working_dir: Optional[str],
                           leg: str = "delegated") -> None:
    if not settings.agent_memory_sync:
        return
    try:
        await asyncio.to_thread(reconcile_memory_sync, project_id, working_dir, leg)
    except Exception:  # noqa: BLE001
        logger.debug("memory_sync: reconcile failed", exc_info=True)


async def push_memory(project_id: Optional[str], working_dir: Optional[str],
                      leg: str = "delegated") -> None:
    if not settings.agent_memory_sync:
        return
    try:
        await asyncio.to_thread(push_memory_sync, project_id, working_dir, leg)
    except Exception:  # noqa: BLE001
        logger.debug("memory_sync: push failed", exc_info=True)

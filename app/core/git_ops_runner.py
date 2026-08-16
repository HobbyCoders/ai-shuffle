"""Git-ops claim loop — this install's executor for the ``git_ops`` queue (D5).

The mirror image of :mod:`app.core.turn_claim`, one layer down: where that loop
claims *turns* placed on this runner, this one claims *git operations* targeted
at this runner. When install A's UI creates a worktree session for install B, A
cannot touch B's disk (there is no device-to-device HTTP in this cluster), so it
writes a ``queued`` ``ensure_worktree`` row into the shared database. B's copy of
this loop notices it, claims it with a single conditional UPDATE, runs
``checkout_manager.ensure_checkout`` locally, and records the resulting path (or
the failure) back on the row.

**Poll-only by design (the v1 ruling).** Unlike ``turn_claim`` there is no
NOTIFY wake here: git ops are rare, not latency-critical, and the one place
where latency WOULD matter — the first turn of a freshly created remote worktree
session racing this loop — is covered by the spawn path's INLINE claim in
:mod:`app.core.query_options`, which claims and executes a matching queued op
itself rather than waiting for a tick. That inline claim and this loop compete
for the same row through ``git_ops.claim_op``; the database picks one winner.

Started from ``startup.install_v2_turns_wiring``, stopped from
``startup.shutdown_services`` — the same lifecycle as the turn-claim loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Correctness cadence — mirrors turn_claim's ≥5s slow poll (OD-1).
CLAIM_POLL_SECONDS = 5.0

# Daily sweep of the durable carrier tables (D8's required cleanup). Kept on
# this loop because it is the only always-on background loop this feature owns.
SWEEP_INTERVAL_SECONDS = 24 * 60 * 60
RETENTION_DAYS = 7

_loop_task: Optional[asyncio.Task] = None
_sweep_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------

def _record_checkout_state(
    project_v2_id: str, path: str, branch: Optional[str]
) -> None:
    """Stamp ``head_sha``/``dirty`` onto the checkout row we just materialized.

    W3-5: this is the ONLY place git subprocesses are added for git-state. The
    hot spawn/refresh paths stay subprocess-free — an op has already paid for a
    ``git worktree add``, so two more cheap reads here are free, whereas a
    per-send ``git status`` would not be.

    Best-effort by construction: ANY failure (git missing, repo in a weird
    state, DB hiccup) is swallowed with a debug log. Populating a display field
    must never turn a successful checkout into a failed op.
    """
    from app.core.config import settings
    from app.core.git_service import git_service
    from app.db import checkouts as checkouts_db

    try:
        rev = git_service._run_git(path, ["rev-parse", "HEAD"], timeout=10)
        head_sha = rev.stdout.strip() if rev.returncode == 0 else None

        st = git_service._run_git(path, ["status", "--porcelain"], timeout=15)
        dirty = bool(st.stdout.strip()) if st.returncode == 0 else None

        if head_sha is None and dirty is None:
            return
        checkouts_db.upsert_checkout(
            project_v2_id,
            settings.effective_instance_id,
            path,
            branch=branch or None,
            head_sha=head_sha,
            dirty=dirty,
        )
    except Exception:  # noqa: BLE001 — display metadata, never the op's problem.
        logger.debug(
            "git_ops: head_sha/dirty capture skipped for %s", path, exc_info=True
        )


def execute_ensure_worktree(op_row: Dict[str, Any]) -> Dict[str, Any]:
    """Run one claimed ``ensure_worktree`` op on THIS install (sync).

    Returns the result payload to record on the row. Raises on a hard failure so
    the caller can settle the op as ``error`` with the message — a swallowed
    exception here would leave the op looking successful with no checkout.

    Shared verbatim by the claim loop and the spawn-path inline claim, so both
    routes produce identical side effects and identical ``result`` shapes.
    """
    from app.core import checkout_manager
    from app.db import projects_v2

    params = op_row.get("params") or {}
    branch = params.get("branch")
    project_v2_id = op_row.get("project_v2_id")

    project = projects_v2.get_project_v2(project_v2_id) if project_v2_id else None
    if not project:
        raise RuntimeError(
            f"ensure_worktree: unknown project_v2 {project_v2_id!r}"
        )

    path = checkout_manager.ensure_checkout(
        project,
        branch,
        base_branch=params.get("base_branch"),
        create_new_branch=bool(params.get("create_new_branch")),
    )
    if not path:
        raise RuntimeError(
            f"Branch '{branch}' could not be materialized on this device "
            f"(no base checkout here, or the branch exists neither locally nor "
            f"on origin)."
        )
    _record_checkout_state(project_v2_id, path, branch)
    return {"path": path, "branch": branch}


_EXECUTORS = {"ensure_worktree": execute_ensure_worktree}


async def run_op(op_row: Dict[str, Any]) -> None:
    """Execute an ALREADY-CLAIMED op and settle it (``done`` or ``error``).

    Never raises: the op row is the record, and a settle failure must not take
    the loop down with it.
    """
    from app.db import git_ops as git_ops_db

    op_id = op_row["id"]
    executor = _EXECUTORS.get(op_row.get("op") or "")
    if executor is None:
        await asyncio.to_thread(
            git_ops_db.fail_op, op_id, f"unknown git op {op_row.get('op')!r}"
        )
        return
    try:
        result = await asyncio.to_thread(executor, op_row)
    except Exception as exc:  # noqa: BLE001 — the failure belongs on the row.
        logger.warning("git_ops: op %s failed: %s", op_id, exc, exc_info=True)
        try:
            await asyncio.to_thread(git_ops_db.fail_op, op_id, str(exc))
        except Exception:  # noqa: BLE001
            logger.warning("git_ops: could not record failure for %s", op_id, exc_info=True)
        return
    try:
        await asyncio.to_thread(git_ops_db.complete_op, op_id, result)
    except Exception:  # noqa: BLE001
        logger.warning("git_ops: could not record result for %s", op_id, exc_info=True)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

async def claim_once() -> int:
    """One claim pass. Returns the number of ops actually executed.

    Never raises: every op is handled independently so one bad row (a deleted
    project, a broken repo) cannot starve the rest.
    """
    me = settings.effective_instance_id
    try:
        from app.db import git_ops as git_ops_db

        queued = await asyncio.to_thread(git_ops_db.list_queued_for_runner, me)
    except Exception:  # noqa: BLE001
        logger.warning("git_ops: queued read failed", exc_info=True)
        return 0

    executed = 0
    for row in queued:
        try:
            claimed = await asyncio.to_thread(git_ops_db.claim_op, row["id"], me)
        except Exception:  # noqa: BLE001
            logger.debug("git_ops: claim failed for %s", row["id"], exc_info=True)
            continue
        if claimed is None:
            # Lost the race (the spawn-path inline claim, or a duplicate loop).
            continue
        logger.info(
            "git_ops: claimed %s (%s) on %s", claimed["id"], claimed.get("op"), me
        )
        await run_op(claimed)
        executed += 1
    return executed


async def claim_loop() -> None:
    """Poll every ``CLAIM_POLL_SECONDS`` and execute this runner's queued ops."""
    logger.info(
        "git_ops: loop started (poll every %ss, runner=%s)",
        int(CLAIM_POLL_SECONDS), settings.effective_instance_id,
    )
    while True:
        try:
            await asyncio.sleep(CLAIM_POLL_SECONDS)
            await claim_once()
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — the loop must survive anything.
            logger.warning("git_ops: pass failed", exc_info=True)
            try:
                await asyncio.sleep(CLAIM_POLL_SECONDS)
            except asyncio.CancelledError:
                break
    logger.info("git_ops: loop stopped")


def sweep_once() -> Dict[str, int]:
    """Purge terminal git_ops rows and stale upload blobs (sync)."""
    from app.db import git_ops as git_ops_db
    from app.db import upload_blobs as upload_blobs_db

    return {
        "git_ops": git_ops_db.purge_terminal(RETENTION_DAYS),
        "upload_blobs": upload_blobs_db.purge_old(RETENTION_DAYS),
    }


async def sweep_loop() -> None:
    """Daily purge of the durable carrier tables. Belt to the DB-maintenance
    task's own 7-day cleanup (``run_database_maintenance``) — either one alone
    is sufficient, and both are idempotent deletes."""
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            purged = await asyncio.to_thread(sweep_once)
            if any(purged.values()):
                logger.info("git_ops: swept %s", purged)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            logger.warning("git_ops: sweep failed", exc_info=True)


def start() -> None:
    """Start the claim + sweep loops on the running event loop (idempotent)."""
    global _loop_task, _sweep_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("git_ops_runner.start(): no running event loop — not started")
        return
    if _loop_task is None or _loop_task.done():
        _loop_task = loop.create_task(claim_loop())
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = loop.create_task(sweep_loop())


async def stop() -> None:
    """Cancel both loops and wait for them to exit (idempotent)."""
    global _loop_task, _sweep_task
    tasks = [t for t in (_loop_task, _sweep_task) if t is not None and not t.done()]
    _loop_task = None
    _sweep_task = None
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — teardown must never raise.
            logger.debug("git_ops: loop raised during shutdown", exc_info=True)

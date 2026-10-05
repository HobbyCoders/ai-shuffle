"""Durable end-of-turn side effects.

Every turn-END path (``SessionRuntime._finalize``, the background query runner)
calls this to perform the DURABLE write a finished turn owes the session row:

1. Parked marker — if the turn ends with live background work (a running
   sub-agent or a background shell), record the pinning kind on the
   session row; otherwise clear it. This is what lets a later boot tell
   "parked awaiting unpark" apart from idle (boot reconcile force-clears
   stale markers, since no background work survives a restart).
   :func:`clear_parked_marker_if_idle` is the between-turns half of the same
   write: the last worker can settle with no wake behind it, and then there is
   no next turn end to do the clearing.

2. Branch resync — the session row's branch was written once, at intake, and
   never again, so an agent that ran ``git checkout -b`` in its own worktree
   left the row (and the sidebar's PR state) naming a branch it had left.
   Turn end reads the checkout's real HEAD and corrects the row when it moved.

3. Summary sidecar flush — the ``session_store_summaries`` fold is COALESCED:
   while a turn streams, at most one fold per ~2s runs inside
   the append path, so the sidecar is normally a little behind when the turn
   ends and stays behind until the next summary READER flushes it. Folding
   here makes it current the moment the turn completes, which is when the
   session list is most likely to be looked at next.

This write is best-effort and never raises: a persistence bug must never
break turn teardown.

Ordering: the parked marker goes FIRST because it is the load-bearing write
(unpark and boot reconcile read it) and it is what a caller waiting on this
coroutine actually needs. The summary flush runs after it, off-loop in a
worker thread, and its failure is swallowed — the fold watermark lives in the
DATABASE, so a skipped flush costs nothing but staleness: the next append or
the next reader folds from exactly where this one stopped.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def resync_session_branch_from_head(session_id: str) -> Optional[Tuple[str, str]]:
    """Point the session row at the branch its checkout is actually on.

    ``sessions.branch`` was only ever written at intake — the user picks a
    branch, the app records it — so an agent that ran a plain
    ``git checkout -b`` inside its own worktree left the row naming a branch it
    had abandoned. Everything downstream then answered about the wrong branch;
    the visible one is the sidebar, which kept showing the old merged PR
    because :func:`app.core.session_pr_state.reconcile_session_pr` looks the PR
    up by the recorded name. One session did this to itself while shipping the
    fix for that exact symptom.

    Blocking (one DB read, one ``git`` head read, and writes only on a real
    change). Returns ``(old, new)`` when it wrote, else ``None``.

    Deliberately narrow:

    - Only a materialized BRANCH checkout. A base checkout's row carries no
      branch of its own, and a ``git checkout -b`` there is the agent moving
      the shared base around, not the session changing identity — adopting it
      would bind the session to a worktree that does not exist and move its cwd
      out from under it.
    - Never a detached or unborn HEAD: there is no branch to record, and the
      previous value is a better answer than none.
    - Never when another checkout on this runner already claims the new name,
      which would make the branch ambiguous to the resolver.
    - The ``checkouts`` row is corrected alongside the session row. It names
      the same stale branch for the same reason, and leaving it behind would
      send the next resolution off to materialize a second worktree.
    """
    from app.core.git_service import git_service
    from app.core.project_resolver import resolve_local_checkout
    from app.db import checkouts as checkouts_db
    from app.db import database

    session = database.get_session(session_id)
    if not session:
        return None

    checkout = resolve_local_checkout(session)
    if not checkout:
        return None

    recorded = (checkout.get("branch") or "").strip()
    path = (checkout.get("path") or "").strip()
    if not recorded or not path or not os.path.isdir(path):
        return None

    # Git walks up to a parent repository when a partially removed worktree
    # leaves an empty directory. Never adopt that parent's HEAD as this session's.
    root = git_service._run_git(path, ["rev-parse", "--show-toplevel"], timeout=10)
    if root.returncode != 0 or not (root.stdout or "").strip():
        return None
    if os.path.normcase(os.path.realpath(root.stdout.strip())) != os.path.normcase(
        os.path.realpath(path)
    ):
        return None

    head = (git_service.get_current_branch(path) or "").strip()
    if not head or head == "HEAD":
        return None

    session_branch = (
        session.get("worktree_branch") or session.get("branch") or ""
    ).strip()
    if head == recorded and head == session_branch:
        return None

    project_id = checkout.get("project_id")
    runner_id = checkout.get("runner_id")
    if not project_id or runner_id is None:
        return None

    existing = checkouts_db.get_checkout(project_id, runner_id, branch=head)
    if existing and os.path.normcase(
        os.path.normpath(existing.get("path") or "")
    ) != os.path.normcase(os.path.normpath(path)):
        logger.info(
            "turn_finalize: not resyncing %s to %r — checkout %s already claims it",
            session_id,
            head,
            existing.get("path"),
        )
        return None

    if head != recorded:
        checkouts_db.upsert_checkout(
            project_id,
            runner_id,
            path,
            branch=head,
            status=checkout.get("status") or "active",
        )
    database.resync_session_branch(session_id, head)
    return (session_branch or recorded, head)


async def _write_parked_task_kind(session_id: str, kind: Optional[str]) -> bool:
    """Write ``sessions.parked_task_kind`` and broadcast the change.

    Shared by the turn-end write and the between-turns clear so both produce
    exactly the same row update and the same ``session.updated`` broadcast.
    Returns whether the row actually changed. Never raises.
    """
    try:
        from app.db import database

        updated = await asyncio.to_thread(
            database.set_session_parked_task_kind, session_id, kind
        )
        if updated:
            try:
                from app.core.sync_engine import sync_engine

                session = await asyncio.to_thread(database.get_session, session_id)
                if session:
                    await sync_engine.broadcast_user_session_updated(
                        user_id=session.get("api_user_id") or "admin",
                        session_id=session_id,
                        updates={"parked_task_kind": kind},
                    )
            except Exception:
                logger.debug("broadcasting parked_task_kind failed", exc_info=True)
        return bool(updated)
    except Exception:
        logger.debug("persisting parked_task_kind failed", exc_info=True)
        return False


async def clear_parked_marker_if_idle(session_id: str) -> bool:
    """Drop ``parked_task_kind`` once nothing is left to park on. Never raises.

    The marker is written at turn end and otherwise cleared only by the NEXT
    turn's end or by a boot reconcile. When the last background worker settles
    BETWEEN turns and no wake fires — a worker that died with its wake already
    suppressed, or one killed by a sweep — there is no next turn, so the marker
    would stay ``agent`` for as long as the process lived.
    ``lifecycle_state._detect_drift`` names that state
    (``parked-marker-while-idle``) but only logs it.

    Deliberately CLEAR-ONLY. Setting the marker is the turn's decision — it
    knows about launches that have no registry record yet — and a terminal-task
    callback that could also SET it would race a turn that is still running.
    Returns True only when a marker was actually removed.
    """
    if not session_id:
        return False
    try:
        from app.core.task_registry import task_registry

        if task_registry.get_parking_background_work(session_id):
            return False
        from app.db import database

        session = await asyncio.to_thread(database.get_session, session_id)
        if not session or not session.get("parked_task_kind"):
            return False
    except Exception:
        logger.debug("parked-marker idle check failed", exc_info=True)
        return False
    cleared = await _write_parked_task_kind(session_id, None)
    if cleared:
        logger.info(
            "turn_finalize: cleared the parked marker on %s — its last "
            "background task settled between turns", session_id,
        )
    return cleared


async def settle_stopped_monitor_parks(session_id: str) -> bool:
    """Settle an idle park whose Monitor wake was deliberately suppressed.

    Called both by per-task Stop and after the park write: Stop can finish
    while finalize's off-thread write has not committed yet. Neither side
    alone closes that race. Other live work (including durable agents) must
    keep its park; this cleanup never schedules a wake.
    """
    try:
        from app.core.task_registry import task_registry
        from app.core import pending_reconcile
        from app.db import turns as turns_db

        if (task_registry.get_parking_background_work(session_id)
                or task_registry.has_unresolved_background_launches(session_id)):
            return False
        if await asyncio.to_thread(pending_reconcile.nonterminal_agent_task_ids, session_id):
            return False
        settled = await asyncio.to_thread(turns_db.settle_parked_turns, session_id)
        await clear_parked_marker_if_idle(session_id)
        return bool(settled)
    except Exception:
        logger.debug("stopped Monitor park cleanup failed for %s", session_id, exc_info=True)
        return False


async def persist_turn_end_state(session_id: str, *, resync_branch: bool = True) -> None:
    """Persist turn-end state; a refused checkout must not adopt another HEAD."""
    # 1. Durable parked marker (set or clear).
    try:
        from app.core.task_registry import task_registry

        work = task_registry.get_parking_background_work(session_id)
        kind = (work[0].get("kind") or "agent") if work else None
        await _write_parked_task_kind(session_id, kind)
    except Exception:
        logger.debug("persisting parked_task_kind failed", exc_info=True)

    # 1a. Settle which branch this session is actually on before anything
    # reads the row. Awaited rather than fired off, because the PR reconcile
    # below looks the PR up BY that branch and would otherwise race it; it is
    # one git head read, and it writes nothing when nothing moved.
    try:
        changed = (
            await asyncio.to_thread(resync_session_branch_from_head, session_id)
            if resync_branch else None
        )
        if changed:
            logger.info(
                "turn_finalize: session %s moved from branch %r to %r",
                session_id,
                changed[0],
                changed[1],
            )
    except Exception:
        logger.debug("session branch resync failed", exc_info=True)

    # 1b. Persist any PR the agent opened/merged this turn so the sidepanel
    # updates without waiting for a GitHub poll. Fire-and-forget: gh must not
    # delay turn teardown.
    try:
        from app.core.session_pr_state import schedule_session_pr_reconcile

        schedule_session_pr_reconcile(session_id, force=True)
    except Exception:
        logger.debug("scheduling PR reconcile failed", exc_info=True)

    # 1c. An agent-queued hand-off to another worker runs once the session is
    # idle. Scheduled, never awaited: it waits for the run to release.
    try:
        from app.core import session_handoff

        session_handoff.schedule(session_id)
    except Exception:
        logger.debug("scheduling session hand-off failed", exc_info=True)

    # 2. Bring the coalesced session-summary fold current for this session.
    #
    # Deliberately LAST: every user-visible part of the turn is already done
    # (the recorder emitted turn.completed before the runtime called us) and
    # the durable marker above — the only write a caller depends on — has
    # landed. This is a no-op with no DB connection at all when nothing is
    # pending for this session, and one short fold transaction when there is,
    # so the added cost is bounded by ~2s of appends, not by transcript size.
    #
    # Never fatal: the fold watermark is in Postgres, so a failed flush is
    # picked up verbatim by the next append or the next summary reader.
    try:
        from app.core import pg_session_store

        await asyncio.to_thread(
            pg_session_store.flush_summary_folds, session_id
        )
    except Exception:
        logger.debug(
            "turn-end summary fold flush failed for %s", session_id, exc_info=True
        )

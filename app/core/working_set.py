"""Working-set sync (V2 Phase 4, Q6).

Transports a session's dirty working tree between runners via a hidden git
scratch ref (``refs/aishuffle/sessions/<session_id>``) pushed to ``origin``,
replacing the legacy cloud stash-shipping. This is the *non-mutating* primitive:
``git stash create`` snapshots the tracked working-tree changes into a dangling
commit WITHOUT touching the tree (byte-identical before/after), so a turn that
finishes on runner A leaves its in-progress edits recoverable when the same
session's next turn lands on runner B.

Everything here is invoked by the CALLER (``session_runtime``'s push/restore
hooks). All network git commands run with ``GIT_TERMINAL_PROMPT=0`` so a
missing credential fails fast (non-zero exit) instead of blocking on a prompt.

Scratch ref only — this module NEVER pushes a branch, and never uses
``git stash push``/``pop`` (those MUTATE the tree, the wrong primitive here).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional

from app.db import sessions as sessions_db

logger = logging.getLogger(__name__)

# Timeouts (seconds), consistent with the git_service helpers.
_LOCAL_TIMEOUT = 30
_NETWORK_TIMEOUT = 60

# Defense-in-depth: session ids are server-generated UUIDs in practice, but the
# id is interpolated into a git ref path, so reject anything that could escape
# the intended ``refs/aishuffle/sessions/<id>`` namespace.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _ref_for(session_id: str) -> str:
    """The hidden scratch ref that carries a session's working set.

    Validates the session id (non-empty, ``^[A-Za-z0-9._-]+$``, no ``..``) and
    raises ``ValueError`` on violation. Both ``snapshot_out`` and ``restore_in``
    wrap their bodies in try/except-log, and ``delete_scratch_ref`` swallows
    everything, so a bad id degrades to a logged no-op rather than emitting a
    malformed ref."""
    if (
        not session_id
        or ".." in session_id
        or not _SAFE_SESSION_ID.match(session_id)
    ):
        raise ValueError(f"unsafe session id for working-set ref: {session_id!r}")
    return f"refs/aishuffle/sessions/{session_id}"


def _run(
    cwd: Path,
    args: List[str],
    *,
    timeout: int = _LOCAL_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run one git command in ``cwd`` — ``git_service._run_git``-style, plus an
    explicit ``GIT_TERMINAL_PROMPT=0`` so network commands never block on a
    credential prompt. Never ``shell=True``; returns the CompletedProcess (the
    caller inspects ``returncode``). Raises only on timeout / spawn failure,
    which the public functions catch."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _is_git_repo(cwd: Path) -> bool:
    try:
        return _run(cwd, ["rev-parse", "--git-dir"], timeout=5).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _has_origin(cwd: Path) -> bool:
    try:
        return _run(cwd, ["remote", "get-url", "origin"], timeout=5).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def snapshot_out(
    session_id: str,
    cwd: Path,
    runner_id: str,
    is_local_origin: bool,
) -> None:
    """Snapshot the dirty tracked working tree into the session's scratch ref and
    push it to ``origin`` (sync; the caller wraps this in ``asyncio.to_thread``).

    Silent no-op when there is nothing to ship or nowhere to ship it: a missing
    or non-git ``cwd``, no ``origin`` remote, a ``local:`` origin (no shared
    remote to sync through), or a clean tracked tree. On any git failure it logs
    and returns — a snapshot must NEVER raise into turn finalization.

    INVARIANT: the working tree is byte-identical before and after this call
    (``git stash create`` records a dangling commit and touches nothing).
    """
    try:
        cwd = Path(cwd)
        if is_local_origin:
            return
        if not cwd.exists() or not _is_git_repo(cwd):
            return
        if not _has_origin(cwd):
            return

        # Tracked changes only (-uno): ``git stash create`` never stashes
        # untracked files, so gating on untracked would push an empty stash.
        status = _run(cwd, ["status", "--porcelain", "-uno"])
        if status.returncode != 0 or not status.stdout.strip():
            return

        created = _run(cwd, ["stash", "create"])
        if created.returncode != 0:
            logger.warning(
                "working_set: stash create failed for %s: %s",
                session_id, created.stderr.strip(),
            )
            return
        sha = created.stdout.strip()
        if not sha:
            return  # nothing stashable

        ref = _ref_for(session_id)
        upd = _run(cwd, ["update-ref", ref, sha])
        if upd.returncode != 0:
            logger.warning(
                "working_set: update-ref failed for %s: %s",
                session_id, upd.stderr.strip(),
            )
            return

        pushed = _run(
            cwd,
            ["push", "--force", "origin", f"{ref}:{ref}"],
            timeout=_NETWORK_TIMEOUT,
        )
        if pushed.returncode != 0:
            logger.warning(
                "working_set: push scratch ref failed for %s: %s",
                session_id, pushed.stderr.strip(),
            )
            return

        sessions_db.set_working_set_ref(
            session_id,
            json.dumps({"ref": ref, "sha": sha, "runner_id": runner_id}),
        )
        logger.info(
            "working_set: snapshotted session %s to %s (runner %s)",
            session_id, ref, runner_id,
        )
    except Exception:
        logger.warning(
            "working_set: snapshot_out failed for %s", session_id, exc_info=True
        )


def restore_in(
    session_id: str,
    cwd: Path,
    runner_id: str,
    stored: str,
    is_local_origin: bool,
) -> Optional[str]:
    """Bring a session's previously-snapshotted working set onto THIS runner's
    clean checkout (sync; caller wraps in ``asyncio.to_thread``).

    Returns a human-readable note string for the caller to surface as a system
    note, or ``None`` when there is nothing to say (same-runner, guard no-op, or
    a silent skip). Only clears the ``working_set_ref`` column on a SUCCESSFUL
    apply — every other outcome keeps the ref so the changes stay recoverable.
    """
    try:
        ref = _ref_for(session_id)
        # 1. Same-runner guard — MUST short-circuit before any git call so a
        #    turn that lands back on the snapshotting runner does no work.
        try:
            meta = json.loads(stored) if stored else None
        except (ValueError, TypeError):
            return None
        if not isinstance(meta, dict):
            return None
        if meta.get("runner_id") == runner_id:
            return None

        # 2. Environment guards — silent (nothing to restore here).
        cwd = Path(cwd)
        if is_local_origin:
            return None
        if not cwd.exists() or not _is_git_repo(cwd):
            return None
        if not _has_origin(cwd):
            return None

        # 3. This checkout must be CLEAN (tracked AND untracked) — we never
        #    overwrite a user's local work.
        status = _run(cwd, ["status", "--porcelain"])
        if status.returncode != 0 or status.stdout.strip():
            return (
                f"Working-set changes from your previous device are available at "
                f"{ref}; this checkout has local changes, so they were not applied "
                f"automatically."
            )

        # 4. Force-update the local scratch ref from origin.
        fetched = _run(
            cwd, ["fetch", "origin", f"+{ref}:{ref}"], timeout=_NETWORK_TIMEOUT
        )
        if fetched.returncode != 0:
            return (
                f"Working-set changes from your previous device are available at "
                f"{ref}; they could not be fetched automatically, so apply them "
                f"manually with: git stash apply {ref}"
            )

        # 5. Locate the stash's first parent (the commit it was based on) and
        #    decide whether this checkout can reach it.
        parent = _run(cwd, ["rev-parse", f"{ref}^"])
        head = _run(cwd, ["rev-parse", "HEAD"])
        if parent.returncode != 0 or head.returncode != 0:
            return (
                f"Working-set changes from your previous device are available at "
                f"{ref}; apply them manually with: git stash apply {ref}"
            )
        p = parent.stdout.strip()
        h = head.stdout.strip()

        if h != p:
            is_ancestor = _run(cwd, ["merge-base", "--is-ancestor", "HEAD", p])
            if is_ancestor.returncode != 0:
                # HEAD is not an ancestor of the stash parent. Distinguish a
                # checkout that is simply AHEAD of the snapshot base (stash
                # parent reachable from HEAD) from a genuine divergence.
                ahead = _run(cwd, ["merge-base", "--is-ancestor", p, "HEAD"])
                if ahead.returncode == 0:
                    return (
                        f"Working-set changes from your previous device are "
                        f"available at {ref}; this checkout is ahead of the point "
                        f"where the changes were saved — apply manually with: "
                        f"git stash apply {ref}"
                    )
                # Genuine divergence — neither is an ancestor of the other.
                return (
                    f"Working-set changes from your previous device are available "
                    f"at {ref}; branches diverged, apply manually with: "
                    f"git stash apply {ref}"
                )
            # Fast-forward the clean checkout up to the stash parent, then apply.
            merged = _run(cwd, ["merge", "--ff-only", p])
            if merged.returncode != 0:
                return (
                    f"Working-set changes from your previous device are available "
                    f"at {ref}; the checkout could not be fast-forwarded, apply "
                    f"manually with: git stash apply {ref}"
                )

        # 6. Apply the stashed working-tree changes (no --index).
        applied = _run(cwd, ["stash", "apply", ref])
        if applied.returncode != 0:
            # The tree was clean before, so reset --hard loses nothing and clears
            # any conflict markers. Keep the ref for a manual apply. NOTE: step 5
            # may have already fast-forwarded HEAD, so we cannot claim "nothing
            # was changed" — only that the working tree is back to a clean state.
            _run(cwd, ["reset", "--hard", "HEAD"])
            return (
                f"Working-set changes from your previous device are available at "
                f"{ref}; automatic apply of your in-progress changes hit "
                f"conflicts, so the working tree was reset back to a clean state "
                f"(the branch may have been fast-forwarded). Apply manually with: "
                f"git stash apply {ref}"
            )

        # 7. Consumed — clear the column and tell the user.
        sessions_db.set_working_set_ref(session_id, None)
        logger.info(
            "working_set: restored session %s from %s onto runner %s",
            session_id, ref, runner_id,
        )
        return "Applied in-progress changes from your previous device."
    except Exception:
        logger.warning(
            "working_set: restore_in failed for %s", session_id, exc_info=True
        )
        return None


def delete_scratch_ref(session_id: str, cwd: Path) -> None:
    """Best-effort removal of a session's scratch ref, local and on ``origin``
    (session hard-delete). Swallows everything — a dead session's leftover ref is
    harmless, so cleanup failure is never surfaced."""
    try:
        ref = _ref_for(session_id)
        cwd = Path(cwd)
        try:
            _run(
                cwd, ["push", "origin", f":{ref}"], timeout=_NETWORK_TIMEOUT
            )
        except Exception:
            logger.debug(
                "working_set: origin scratch-ref delete failed for %s",
                session_id, exc_info=True,
            )
        try:
            _run(cwd, ["update-ref", "-d", ref])
        except Exception:
            logger.debug(
                "working_set: local scratch-ref delete failed for %s",
                session_id, exc_info=True,
            )
    except Exception:
        logger.debug(
            "working_set: delete_scratch_ref failed for %s", session_id, exc_info=True
        )

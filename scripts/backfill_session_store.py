"""
Backfill local JSONL session transcripts into the Postgres SessionStore.

This is the operational companion to PR A (the ``PgSessionStore`` mirror) and
PR C (the ``postgres_only`` cutover). The live mirror only captures sessions
that stream *after* ``session_store_mode`` is flipped to ``"mirror"``; every
session that already exists on disk as JSONL predates the mirror and has zero
rows in ``session_store_entries``. This script reads those local JSONL
transcripts and writes them into Postgres in the **exact same row shape** the
live mirror produces, so the two are indistinguishable to readers and to the
drift verifier (``app/core/session_store_verify.py``).

What it writes
--------------
For each session it locates on disk:

* the main transcript  -> rows with ``subpath IS NULL``
* every subagent transcript (``subagents/agent-<id>``) -> rows with that subpath

Each row's ``entry`` column is the raw JSONL line object (a dict), passed
through verbatim — identical to ``PgSessionStore._append_sync``. We do NOT
hand-roll the INSERT: we call ``PgSessionStore.backfill()`` so the column set,
JSON encoding, summary-sidecar fold, and ordering all match the live path.

project_key derivation (IMPORTANT)
----------------------------------
The live mirror derives ``project_key`` from the on-disk transcript *file path*
(SDK ``file_path_to_session_key`` -> the project-directory folder name, e.g.
``-workspace`` or ``D--Projects-ai-shuffle``). It does NOT use
``project_key_for_directory(cwd)`` (which realpath-canonicalizes and can differ
per machine — on Windows ``/workspace`` realpaths to ``D:\\workspace`` ->
``D--workspace``). To match the mirror byte-for-byte we therefore take
``project_key`` from the **resolved JSONL file's parent directory name**
(``jsonl_path.parent.name``) rather than recomputing it from the working dir.

Idempotency
-----------
``session_store_entries`` has a ``BIGSERIAL`` primary key and **no** unique
constraint on ``(project_key, session_id, subpath)`` — so there is no
``ON CONFLICT`` target and a naive re-INSERT would duplicate rows. Idempotency
is therefore a **pre-check skip**: ``PgSessionStore.backfill(skip_if_present=
True)`` counts existing rows for the exact ``(project_key, session_id, subpath)``
and writes nothing if any already exist. The on-disk JSONL is the complete,
append-only history, so "any rows present" safely means "already done". Re-runs
are no-ops for already-backfilled (project_key, session_id, subpath) tuples.

Per-machine / shared-DB topology
--------------------------------
JSONL transcripts are LOCAL to each machine, but the Postgres database is
SHARED (the Windows primary and the Linux worker at /workspace both write one
DB). This script backfills only the JSONL present on the machine it runs on,
so it must be run on BOTH machines to cover every transcript. Running it on
both against the same DB is safe: the idempotency pre-check makes overlapping
sessions (if any) no-ops on whichever machine runs second.

Usage
-----
From the repo root, with DATABASE_URL pointing at the target Postgres::

    # See what WOULD be written, touch nothing:
    python -m scripts.backfill_session_store --dry-run

    # Backfill everything, gently (throttled, batched):
    python -m scripts.backfill_session_store --batch-size 500 --sleep-ms 50

    # One session only:
    python -m scripts.backfill_session_store --session <sdk_session_id>

    # First N sessions (smoke test):
    python -m scripts.backfill_session_store --limit 20 --dry-run

    # Backfill, then run the drift verifier over what was written:
    python -m scripts.backfill_session_store --verify

    # Verify only (no writes) — reuses app/core/session_store_verify.py:
    python -m scripts.backfill_session_store --verify-only

This script bootstraps the DB pool itself and is meant to be run manually,
coordinated with a human operator. It does NOT modify ``session_store_mode``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backfill_session_store")


# ---------------------------------------------------------------------------
# Result accounting
# ---------------------------------------------------------------------------


@dataclass
class SessionResult:
    """Outcome of backfilling a single session."""

    sdk_session_id: str
    working_dir: str
    project_key: Optional[str] = None
    status: str = "skipped"  # written | already | skipped | error
    main_rows: int = 0
    subagent_count: int = 0
    subagent_rows: int = 0
    detail: str = ""


@dataclass
class RunSummary:
    total_sessions: int = 0
    written: int = 0
    already: int = 0
    skipped: int = 0
    errors: int = 0
    main_rows: int = 0
    subagent_rows: int = 0
    results: List[SessionResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Session enumeration
# ---------------------------------------------------------------------------


def _enumerate_sessions(
    only_session: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return session rows that have an ``sdk_session_id``.

    Runs its own SELECT against the ``sessions`` table rather than going
    through ``get_sessions(...)`` (which layers instance/limit filters we do
    not want for an exhaustive backfill). Returns enough columns for
    ``resolve_working_dir`` to do its job (worktree_id / project_id).
    """
    from app.db.base import get_db

    where = "sdk_session_id IS NOT NULL AND sdk_session_id <> ''"
    params: list = []
    if only_session:
        where += " AND sdk_session_id = %s"
        params.append(only_session)

    sql = (
        "SELECT id, sdk_session_id, project_id, worktree_id "
        "FROM sessions "
        f"WHERE {where} "
        "ORDER BY created_at ASC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def _resolve_working_dir(session: Dict[str, Any]) -> str:
    """Resolve the on-disk working directory for a session row.

    Reuses ``message_resolver.resolve_working_dir`` (worktree -> project ->
    default workspace). Falls back to the default workspace if resolution
    raises (e.g. a project/worktree row was deleted) so one bad row never
    aborts the whole run.
    """
    from app.core.message_resolver import resolve_working_dir, _default_working_dir

    try:
        return resolve_working_dir(session)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "working_dir resolution failed for %s (%s); using default workspace",
            session.get("sdk_session_id"),
            e,
        )
        return _default_working_dir()


# ---------------------------------------------------------------------------
# JSONL -> entries
# ---------------------------------------------------------------------------


def _read_entries(jsonl_path: Path) -> List[Dict[str, Any]]:
    """Read all line objects from a JSONL transcript.

    Uses ``parse_jsonl_file`` (which already skips blank/corrupt lines and
    never raises on a read error — it logs and yields nothing), so a partially
    unreadable transcript degrades gracefully instead of aborting.
    """
    from app.core.jsonl_parser import parse_jsonl_file

    return [dict(e) for e in parse_jsonl_file(jsonl_path)]


def _project_key_for(jsonl_path: Path) -> str:
    """The project_key the live mirror would write for ``jsonl_path``.

    Matches SDK ``file_path_to_session_key``: the project_key is the on-disk
    project-directory folder name, i.e. the parent dir of the main transcript.
    """
    return jsonl_path.parent.name


async def _backfill_one(
    store: Any,
    session: Dict[str, Any],
    *,
    batch_size: int,
    dry_run: bool,
) -> SessionResult:
    """Backfill a single session's main + subagent transcripts."""
    from app.core.jsonl_parser import get_agent_jsonl_paths, get_session_jsonl_path

    sdk_session_id = session["sdk_session_id"]
    working_dir = _resolve_working_dir(session)
    res = SessionResult(sdk_session_id=sdk_session_id, working_dir=working_dir)

    main_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not main_path:
        res.status = "skipped"
        res.detail = "no main JSONL transcript on disk"
        logger.info("SKIP %s: %s", sdk_session_id, res.detail)
        return res

    project_key = _project_key_for(main_path)
    res.project_key = project_key

    main_entries = _read_entries(main_path)
    if not main_entries:
        res.status = "skipped"
        res.detail = f"main transcript empty/unreadable ({main_path})"
        logger.info("SKIP %s: %s", sdk_session_id, res.detail)
        return res

    # Subagent transcripts (nested or legacy-flat layout). Missing/none is fine.
    try:
        agent_paths = get_agent_jsonl_paths(sdk_session_id, working_dir) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("subagent discovery failed for %s: %s", sdk_session_id, e)
        agent_paths = {}
    res.subagent_count = len(agent_paths)

    main_key = {"project_key": project_key, "session_id": sdk_session_id}

    if dry_run:
        # Report what WOULD be written without touching the DB.
        res.status = "written"
        res.main_rows = len(main_entries)
        for agent_id, agent_path in agent_paths.items():
            res.subagent_rows += len(_read_entries(agent_path))
        res.detail = "dry-run (no writes)"
        logger.info(
            "DRY %s [%s]: main=%d rows, subagents=%d (%d rows)",
            sdk_session_id, project_key, res.main_rows,
            res.subagent_count, res.subagent_rows,
        )
        return res

    # --- main transcript ---
    wrote_main = await store.backfill(
        main_key, main_entries, skip_if_present=True, batch_size=batch_size
    )
    res.main_rows = wrote_main

    # --- subagent transcripts ---
    for agent_id, agent_path in agent_paths.items():
        sub_entries = _read_entries(agent_path)
        if not sub_entries:
            logger.info(
                "  subagent %s for %s empty/unreadable; skipped",
                agent_id, sdk_session_id,
            )
            continue
        sub_key = {
            "project_key": project_key,
            "session_id": sdk_session_id,
            "subpath": f"subagents/agent-{agent_id}",
        }
        res.subagent_rows += await store.backfill(
            sub_key, sub_entries, skip_if_present=True, batch_size=batch_size
        )

    if wrote_main == 0 and res.subagent_rows == 0:
        # Nothing written because every (project_key, session_id, subpath)
        # already had rows — idempotent re-run / already-mirrored.
        res.status = "already"
        res.detail = "all transcripts already present (idempotent skip)"
        logger.info("ALREADY %s [%s]: nothing to write", sdk_session_id, project_key)
    else:
        res.status = "written"
        logger.info(
            "WROTE %s [%s]: main=%d rows, subagents=%d (%d rows)",
            sdk_session_id, project_key, res.main_rows,
            res.subagent_count, res.subagent_rows,
        )
    return res


# ---------------------------------------------------------------------------
# Verification (reuses app/core/session_store_verify.py)
# ---------------------------------------------------------------------------


def verify_session_store_backfill(
    sessions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run the drift verifier across ``sessions`` and summarize.

    Reuses ``app/core/session_store_verify.py`` rather than reimplementing the
    comparison:

    * Each session is run through ``log_session_store_drift`` so the same
      ``[StoreVerify]`` log lines a live mirror-mode load would emit are
      produced (temporarily forcing ``session_store_mode == "mirror"`` because
      that function is a no-op outside mirror mode).
    * For a machine-readable pass/fail tally we also call the verifier's own
      DB/JSONL comparison helpers (``_db_main_uuids`` /
      ``_db_subagent_subpaths`` + ``parse_jsonl_file`` / ``get_agent_jsonl_paths``)
      and classify each session as OK / DRIFT / EMPTY.

    Returns a dict summary with explicit OK and DRIFT lists.
    """
    from app.core import session_store_verify as ssv
    from app.core.config import settings
    from app.core.jsonl_parser import (
        get_agent_jsonl_paths,
        get_session_jsonl_path,
        parse_jsonl_file,
    )

    ok: List[str] = []
    drift: List[str] = []
    empty: List[str] = []

    # Force mirror mode so log_session_store_drift actually runs; restore after.
    prev_mode = settings.session_store_mode
    settings.session_store_mode = "mirror"
    try:
        for session in sessions:
            sid = session["sdk_session_id"]
            working_dir = _resolve_working_dir(session)

            # Human-readable [StoreVerify] log line (same as a live load).
            ssv.log_session_store_drift(sid, working_dir)

            # Structured classification via the SAME helpers the verifier uses.
            try:
                db_uuids = ssv._db_main_uuids(sid)
                if not db_uuids:
                    empty.append(sid)
                    continue
                jsonl_path = get_session_jsonl_path(sid, working_dir)
                if not jsonl_path:
                    drift.append(f"{sid}: DB rows but no JSONL on disk")
                    continue
                jsonl_uuids = [e.get("uuid") for e in parse_jsonl_file(jsonl_path)]
                db_subpaths = ssv._db_subagent_subpaths(sid)
                jsonl_agents = get_agent_jsonl_paths(sid, working_dir) or {}

                if len(db_uuids) != len(jsonl_uuids):
                    drift.append(
                        f"{sid}: count db={len(db_uuids)} jsonl={len(jsonl_uuids)}"
                    )
                elif all(db_uuids) and all(jsonl_uuids) and db_uuids != jsonl_uuids:
                    drift.append(f"{sid}: uuid/order mismatch")
                elif len(db_subpaths) != len(jsonl_agents):
                    drift.append(
                        f"{sid}: subagents db={len(db_subpaths)} "
                        f"jsonl={len(jsonl_agents)}"
                    )
                else:
                    ok.append(sid)
            except Exception as e:  # pragma: no cover - defensive
                drift.append(f"{sid}: verify error: {e}")
    finally:
        settings.session_store_mode = prev_mode

    return {
        "total": len(sessions),
        "ok": ok,
        "drift": drift,
        "empty": empty,
    }


def _print_verify_summary(summary: Dict[str, Any]) -> None:
    print("\n===== Drift verification =====")
    print(f"  sessions checked : {summary['total']}")
    print(f"  OK               : {len(summary['ok'])}")
    print(f"  EMPTY (no DB rows): {len(summary['empty'])}")
    print(f"  DRIFT            : {len(summary['drift'])}")
    if summary["drift"]:
        print("  --- DRIFT detail ---")
        for line in summary["drift"]:
            print(f"    ! {line}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_backfill(args: argparse.Namespace) -> RunSummary:
    """Enumerate, backfill, and (optionally) verify."""
    from app.core.pg_session_store import PgSessionStore

    sessions = _enumerate_sessions(only_session=args.session, limit=args.limit)
    summary = RunSummary(total_sessions=len(sessions))
    logger.info(
        "Enumerated %d session(s)%s%s",
        len(sessions),
        f" (session={args.session})" if args.session else "",
        f" (limit={args.limit})" if args.limit else "",
    )

    if not args.verify_only:
        store = PgSessionStore()
        for i, session in enumerate(sessions, 1):
            try:
                res = await _backfill_one(
                    store, session,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                )
            except Exception as e:
                res = SessionResult(
                    sdk_session_id=session.get("sdk_session_id", "?"),
                    working_dir="",
                    status="error",
                    detail=str(e),
                )
                logger.error("ERROR backfilling %s: %s", res.sdk_session_id, e)

            summary.results.append(res)
            if res.status == "written":
                summary.written += 1
            elif res.status == "already":
                summary.already += 1
            elif res.status == "error":
                summary.errors += 1
            else:
                summary.skipped += 1
            summary.main_rows += res.main_rows
            summary.subagent_rows += res.subagent_rows

            # Throttle between sessions so a bulk backfill stays gentle on a
            # shared/production DB. Skipped during dry-run (no DB load).
            if args.sleep_ms and not args.dry_run and i < len(sessions):
                await asyncio.sleep(args.sleep_ms / 1000.0)

    if args.verify or args.verify_only:
        verify_summary = verify_session_store_backfill(sessions)
        _print_verify_summary(verify_summary)

    return summary


def _print_summary(summary: RunSummary, dry_run: bool) -> None:
    label = "DRY-RUN " if dry_run else ""
    print(f"\n===== {label}Backfill summary =====")
    print(f"  sessions enumerated : {summary.total_sessions}")
    print(f"  written             : {summary.written}")
    print(f"  already present     : {summary.already}")
    print(f"  skipped (no JSONL)  : {summary.skipped}")
    print(f"  errors              : {summary.errors}")
    print(f"  main rows           : {summary.main_rows}")
    print(f"  subagent rows       : {summary.subagent_rows}")
    if summary.errors:
        print("  --- errors ---")
        for r in summary.results:
            if r.status == "error":
                print(f"    ! {r.sdk_session_id}: {r.detail}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.backfill_session_store",
        description=(
            "Backfill local JSONL session transcripts into the Postgres "
            "SessionStore (session_store_entries), in the same row shape the "
            "live mirror produces. Idempotent, batched, throttled."
        ),
        epilog=(
            "JSONL is LOCAL per machine but the DB is SHARED - run this on every "
            "machine that holds transcripts (e.g. Windows primary AND the Linux "
            "worker at /workspace). Re-runs and cross-machine overlap are safe: "
            "the idempotency pre-check skips (project_key, session_id, subpath) "
            "tuples that already have rows. This script does NOT change "
            "session_store_mode."
        ),
    )
    p.add_argument(
        "--session", metavar="SDK_SESSION_ID",
        help="Backfill only this one SDK session id.",
    )
    p.add_argument(
        "--limit", type=int, metavar="N",
        help="Backfill at most the first N enumerated sessions (oldest first).",
    )
    p.add_argument(
        "--batch-size", type=int, default=500, metavar="N",
        help="Rows per INSERT batch within a single transcript (default: 500).",
    )
    p.add_argument(
        "--sleep-ms", type=int, default=0, metavar="MS",
        help="Sleep this many milliseconds between sessions to throttle a "
             "bulk run on a shared DB (default: 0).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Enumerate and report what WOULD be written; touch no DB rows.",
    )
    p.add_argument(
        "--verify", action="store_true",
        help="After backfilling, run the drift verifier over all sessions and "
             "print a DB<->JSONL match summary.",
    )
    p.add_argument(
        "--verify-only", action="store_true",
        help="Skip writing; only run the drift verifier (read-only).",
    )
    p.add_argument(
        "--log-level", default="INFO",
        help="Python logging level (default: INFO).",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Bootstrap the DB pool. The app normally does this at startup
    # (app/core/startup.py init_core); a standalone script must do it itself.
    from app.db.base import close_pool, init_pool

    init_pool(min_conn=1, max_conn=4)
    try:
        summary = asyncio.run(run_backfill(args))
        _print_summary(summary, args.dry_run)
    finally:
        close_pool()

    # Non-zero exit if any session errored, so CI / operators notice.
    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())

"""Tests for the boot-time reconcile-from-durable-truth phase (Phase 2.4).

Covers:
- clearing this install's orphaned parked markers (with an error_log entry);
- resetting drifted live-looking ``sessions.status`` values;
- never touching another install's sessions (instance scoping);
- the durable parked marker being set/cleared at the turn-end finalize seam
  (V2: ``turn_finalize.persist_turn_end_state`` is the ONLY marker writer —
  the live store and its start/end_streaming seams are deleted);
- reconcile failures never raising out of startup;
- schema parity + the v51 ``parked_task_kind`` column (fresh schema and the
  idempotent column migration from a v50-shaped database).
"""

import uuid

import pytest

from app.core.config import settings
from app.db import database
from app.core.startup_reconcile import reconcile_from_durable_truth


def _make_session(test_db, *, instance_id=None, parked_task_kind=None,
                  status="active"):
    """Insert a session row and return its id."""
    session_id = f"reconcile-{uuid.uuid4().hex[:12]}"
    database.create_session(session_id=session_id, profile_id="default")
    test_db.execute(
        "UPDATE sessions SET instance_id = ?, parked_task_kind = ?, status = ? "
        "WHERE id = ?",
        (instance_id or settings.effective_instance_id, parked_task_kind,
         status, session_id),
    )
    test_db.commit()
    return session_id


def _session_row(test_db, session_id):
    row = test_db.execute(
        "SELECT status, parked_task_kind, instance_id FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None
    return row


def _error_log_rows(test_db, session_id):
    return test_db.execute(
        "SELECT source, level, message FROM error_log WHERE session_id = ?",
        (session_id,),
    ).fetchall()


# =============================================================================
# Branch (a): orphaned session state, scoped to this install
# =============================================================================

async def test_orphaned_parked_marker_cleared_with_error_log(mock_db, test_db):
    session_id = _make_session(test_db, parked_task_kind="agent")

    counts = await reconcile_from_durable_truth()

    assert counts["parked_markers_cleared"] == 1
    row = _session_row(test_db, session_id)
    assert row["parked_task_kind"] is None
    assert row["status"] == "active"

    errors = _error_log_rows(test_db, session_id)
    assert len(errors) == 1
    assert errors[0]["source"] == "backend"
    assert errors[0]["level"] == "WARNING"
    assert "parked" in errors[0]["message"]
    assert session_id in errors[0]["message"]


async def test_orphaned_live_looking_status_reset_with_error_log(mock_db, test_db):
    session_id = _make_session(test_db, status="streaming")

    counts = await reconcile_from_durable_truth()

    assert counts["statuses_reset"] == 1
    assert _session_row(test_db, session_id)["status"] == "active"

    errors = _error_log_rows(test_db, session_id)
    assert len(errors) == 1
    assert "streaming" in errors[0]["message"]


async def test_other_instance_sessions_untouched(mock_db, test_db):
    # Another install's session may be legitimately parked/streaming right now.
    foreign_id = _make_session(
        test_db, instance_id="some-other-install-1234",
        parked_task_kind="agent", status="streaming",
    )
    own_id = _make_session(test_db, parked_task_kind="shell")

    counts = await reconcile_from_durable_truth()

    assert counts["parked_markers_cleared"] == 1
    assert counts["statuses_reset"] == 0
    foreign = _session_row(test_db, foreign_id)
    assert foreign["parked_task_kind"] == "agent"
    assert foreign["status"] == "streaming"
    assert _error_log_rows(test_db, foreign_id) == []
    assert _session_row(test_db, own_id)["parked_task_kind"] is None


async def test_idle_sessions_untouched_and_nothing_reconciled(mock_db, test_db):
    session_id = _make_session(test_db)  # active, no marker

    counts = await reconcile_from_durable_truth()

    assert counts == {
        "parked_markers_cleared": 0,
        "statuses_reset": 0,
        "stray_cli_reaped": 0,
        "background_task_rows_killed": 0,
        "turns_reaped": 0,
    }
    assert _session_row(test_db, session_id)["status"] == "active"
    assert _error_log_rows(test_db, session_id) == []


async def test_archived_status_is_terminal_and_untouched(mock_db, test_db):
    session_id = _make_session(test_db, status="archived")

    counts = await reconcile_from_durable_truth()

    assert counts["statuses_reset"] == 0
    assert _session_row(test_db, session_id)["status"] == "archived"


# =============================================================================
# Durable parked marker: set/cleared at the turn-end finalize seam
#
# V2: ``persist_turn_end_state`` is the only marker writer. There is NO
# start-of-turn clear seam anymore (the live store's start_streaming clear is
# deleted): a stale marker is cleared either by the NEXT turn's own finalize
# (no work left → kind=None, pinned below) or by the reconcilers.
# =============================================================================

async def test_turn_end_with_background_agent_sets_marker(mock_db, test_db):
    from app.core.task_registry import task_registry
    from app.core.turn_finalize import persist_turn_end_state

    session_id = _make_session(test_db)
    task_registry.start_task(session_id, "task-1", "agent", "background research")
    try:
        await persist_turn_end_state(session_id)
    finally:
        task_registry.clear_session(session_id)

    assert _session_row(test_db, session_id)["parked_task_kind"] == "agent"


async def test_turn_end_without_background_work_clears_marker(mock_db, test_db):
    from app.core.task_registry import task_registry
    from app.core.turn_finalize import persist_turn_end_state

    session_id = _make_session(test_db, parked_task_kind="agent")
    task_registry.start_task(session_id, "task-1", "agent", "background research")
    task_registry.complete_task(session_id, "task-1", "completed")
    try:
        await persist_turn_end_state(session_id)
    finally:
        task_registry.clear_session(session_id)

    assert _session_row(test_db, session_id)["parked_task_kind"] is None


async def test_plain_shell_task_does_not_park(mock_db, test_db):
    # Monitor-style shells (kind="shell", not SDK-backgrounded) do not pin the
    # session in the pool, so they must not set the durable parked marker.
    from app.core.task_registry import task_registry
    from app.core.turn_finalize import persist_turn_end_state

    session_id = _make_session(test_db)
    task_registry.start_task(session_id, "shell-1", "shell", "npm run build")
    try:
        await persist_turn_end_state(session_id)
    finally:
        task_registry.clear_session(session_id)

    assert _session_row(test_db, session_id)["parked_task_kind"] is None


# =============================================================================
# Reconcile failure never raises out of startup
# =============================================================================

async def test_reconcile_db_failure_does_not_raise(mock_db, monkeypatch):
    import app.core.startup_reconcile as sr

    def _boom():
        raise RuntimeError("database exploded")

    monkeypatch.setattr(sr, "get_db", _boom)

    counts = await reconcile_from_durable_truth()

    assert counts == {
        "parked_markers_cleared": 0,
        "statuses_reset": 0,
        "stray_cli_reaped": 0,
        "background_task_rows_killed": 0,
        "turns_reaped": 0,
    }


async def test_stray_cli_sweep_suppressed_under_testing(mock_db, monkeypatch):
    # TESTING=1 (set by conftest) must keep the process-table sweep out of the
    # suite even when the flag is on.
    import app.core.startup_reconcile as sr

    def _fail():  # pragma: no cover — must not be reached
        raise AssertionError("sweep must not run under TESTING")

    monkeypatch.setattr(sr, "_sweep_stray_cli_processes", _fail)
    monkeypatch.setattr(settings, "reap_stray_cli_on_boot", True)

    counts = await reconcile_from_durable_truth()

    assert counts["stray_cli_reaped"] == 0


# =============================================================================
# Stale background_tasks rows: flip + emit terminal task.updated frames
#
# Flipping the durable row is not enough for the UI — the frontend replays
# ``session_events`` and only settles a background-task chip when a terminal
# ``task.updated`` frame arrives. Boot reconcile must emit one per flipped row.
# =============================================================================

def _seed_bg_task(session_id, task_id, status, *, tool_use_id=None):
    from app.db.background_tasks import upsert_background_task
    upsert_background_task({
        "task_id": task_id,
        "session_id": session_id,
        "kind": "agent",
        "tool_use_id": tool_use_id,
        "title": task_id,
        "status": status,
    })


async def test_stale_background_rows_emit_terminal_frames(
    mock_db, test_db, monkeypatch
):
    import app.core.turn_recorder as tr

    sid = _make_session(test_db)
    _seed_bg_task(sid, "t-a", "running", tool_use_id="toolu_a")
    _seed_bg_task(sid, "t-b", "running", tool_use_id=None)
    _seed_bg_task(sid, "t-done", "completed", tool_use_id="toolu_done")

    calls = []

    async def _capture(session_id, event):
        calls.append((session_id, event))

    monkeypatch.setattr(tr, "record_drained_event", _capture)

    counts = await reconcile_from_durable_truth()

    assert counts["background_task_rows_killed"] == 2
    assert len(calls) == 2

    by_task = {ev["data"]["task_id"]: (sid_arg, ev) for sid_arg, ev in calls}
    assert set(by_task) == {"t-a", "t-b"}
    for task_id, (sid_arg, ev) in by_task.items():
        assert sid_arg == sid
        assert ev["type"] == "system"
        assert ev["subtype"] == "task_updated"
        assert ev["content"] == "Background task lost in a server restart"
        assert ev["data"]["status"] == "killed"
        assert ev["data"]["patch"] == {"status": "killed"}
        assert ev["data"]["reason"] == "server_restart"
    # tool_use_id present only when the row carried one.
    assert by_task["t-a"][1]["data"]["tool_use_id"] == "toolu_a"
    assert "tool_use_id" not in by_task["t-b"][1]["data"]


async def test_stale_background_emit_failure_does_not_block_others(
    mock_db, test_db, monkeypatch
):
    import app.core.turn_recorder as tr

    sid = _make_session(test_db)
    _seed_bg_task(sid, "t-a", "running")
    _seed_bg_task(sid, "t-b", "running")

    attempted = []

    async def _flaky(session_id, event):
        task_id = event["data"]["task_id"]
        attempted.append(task_id)
        if task_id == "t-a":
            raise RuntimeError("emit boom")

    monkeypatch.setattr(tr, "record_drained_event", _flaky)

    counts = await reconcile_from_durable_truth()

    # One row's emission raising must not prevent the other, nor the count.
    assert counts["background_task_rows_killed"] == 2
    assert set(attempted) == {"t-a", "t-b"}


# =============================================================================
# V2 turns — boot reap-and-kick sweep (D3)
# =============================================================================

async def test_boot_reap_requeues_dead_lease_turns_and_kicks(
    mock_db, test_db, monkeypatch
):
    from app.core import turn_scheduler
    from app.db import turns as turns_db

    kicked = []
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: kicked.append(sid))
    tid = turns_db.enqueue_turn("boot-s1", "user", prompt="p")["id"]
    turns_db.start_turn(tid, run_id="run-missing")  # no runs row → dead lease

    counts = await reconcile_from_durable_truth()

    assert counts["turns_reaped"] == 1
    assert turns_db.get_turn(tid)["state"] == "queued"  # user turn requeued
    assert kicked == ["boot-s1"]


# =============================================================================
# Schema: version parity + parked_task_kind column
# =============================================================================

#: Lowest schema version this build is known to be at. The point of this test
#: is PARITY between the two modules that declare the version — the exact number
#: is recorded in database.py's changelog comment, which is the single place a
#: bump has to be documented, so this floor only needs raising when a migration
#: needs a test to prove it landed (those live next to the migration).
MIN_SCHEMA_VERSION = 84


def test_schema_version_parity():
    from app.db.database import SCHEMA_VERSION as db_version
    from app.db.schema_postgres import SCHEMA_VERSION as pg_version

    # database.py and schema_postgres.py must agree on the schema version.
    # v84 is the performance pass (docs/PERFORMANCE_REVIEW_2026-08.md):
    # idx_usage_log_session (B3), sessions.is_worker (B7),
    # session_store_summaries.folded_through_id (B4) and idx_projects_instance
    # (B11). All additive and idempotent; see tests/db/test_perf_migrations_v84.py.
    # v83 was fluid_sessions.project_root: the project the session ran
    # in (fluid's ``project_root_for(cwd)``), recorded by
    # PgFluidStore.create_session so SessionSearch can scope cross-session
    # recall to one project. NULL on every pre-existing row = "unknown
    # project", which recall never excludes. Additive/nullable, guarded ADD
    # COLUMN alongside cli_session_id.
    # v82 was the tool-access rulings T2/T5: _migrate_agent_tool_access
    # stamps ``tool_selection_mode`` into every agents.config and folds the two
    # retired switches (``ai_tools`` / ``app_config_tools``) into the tool
    # lists, so the runtime never infers intent from list emptiness again. No
    # DDL — a JSONB data migration in the always-run column-migration pass,
    # idempotent because a config already carrying a valid mode is skipped.
    # v81 was sessions.fast, the per-session fast-mode toggle that
    # rides alongside the rest of the run binding: TRUE asks the leg for its
    # expedited service (the Claude CLI's fastMode setting on the delegated
    # lane, service_tier: priority on the OpenAI Responses one), and NULL —
    # every pre-existing row — runs standard, so an upgrade changes nobody's
    # speed or price.
    # v80 added sessions.context_window, the explicit input-window limit
    # selected alongside the v79 model/effort/permission run binding. Agents stopped
    # pinning them, so the session row is where they live; NULL on every
    # pre-existing row, which falls back to launched_model and then the agent's
    # legacy config. v78 was one-time pairing grants, scoped controller
    # sessions, mobile credential lifetime/revocation, and runner approval.
    # v77 was the
    # harness rename omni -> fluid: omni_sessions /
    # omni_session_nodes / omni_session_events renamed in place to fluid_*
    # by _migrate_omni_tables_to_fluid(), which runs before the bootstrap DDL
    # so the idempotent CREATE TABLE IF NOT EXISTS fluid_* can never strand
    # the old tables' rows. v76 added the two cross-install carrier tables (docs/
    # DEVICE_GIT_CONTROL_PLAN.md, W2): ``git_ops`` — the durable git-operation
    # queue each install claims ops off (D5; first op ``ensure_worktree``, whose
    # params row is the durable carrier of the caller's create_new_branch
    # intent) — and ``upload_blobs`` — DB-mediated upload delivery modeled on
    # note_attachments, so an attachment staged on one install's disk reaches a
    # turn executing on another (D8). Both are new tables landed by guarded
    # idempotent blocks in _run_column_migrations. v75 added sessions.hidden:
    # the list-visibility flag — TRUE keeps
    # a session out of the sessions-list queries (wake-word "command" voice
    # sessions are born hidden; the row stays fully fetchable by id), NULL/
    # FALSE (every pre-existing row) stays visible. Additive/nullable with no
    # index, the sessions.mode template. v74 added sessions.mode: the
    # session's interaction mode — 'chat'
    # (the implicit default; NULL means chat, so every pre-existing session is
    # unaffected) or 'voice' (an OpenAI Realtime voice call, ruling VM4 of
    # docs/VOICE_MODE_PLAN.md, wave VW1). Additive/nullable with no index, the
    # sessions.provider_id template. v73 added model_providers.api_dialect: the
    # wire format a custom
    # row's endpoint speaks — 'anthropic' (default when NULL; Messages at
    # {base_url}/v1/messages) or 'openai' (chat/completions — xAI-direct /
    # Groq / vLLM), read by the harness adapter's leg decision. v72 added
    # pending_wakeups.model: the scheduling turn's effective
    # model, replayed as the wake query's model override so a wake no longer
    # fires on the profile default and re-primes the prompt cache on the wrong
    # model. v71 was the agents unification (ruling A4): ``profiles`` renamed
    # in place to ``agents`` (FKs follow), ``subagents`` rows folded into
    # ``agents`` and the table dropped. v70 added fluid_sessions.cli_session_id:
    # the delegated engine's ``claude --resume`` handle, which is a CLI-issued
    # uuid and NOT the fluid session id. It used to live only on the in-process
    # engine, so after a restart the adapter resumed the CLI with the fluid
    # session id and the turn died immediately. v69 added the fluid harness
    # session store (fluid_sessions /
    # fluid_session_nodes / fluid_session_events — PG-backed branching node
    # trees + durable event log for AIH_OMNI_STORE=pg; purely additive). v68
    # added the pending_wakeups table (#598 — durable per-session
    # wakeups so ScheduleWakeup / idle-at-edge watchdog nudges survive an app
    # restart instead of dropping the bare in-process asyncio timer). v67 added
    # the provider_usage_snapshots table (PROVIDER_USAGE_CONTEXT
    # _PLAN.md U1 — one row per non-Claude provider holding plan-usage windows,
    # credits and an allowlisted raw payload; additive, distinct from the
    # per-install usage_snapshots Claude path). v66 added the per-session provider
    # binding (nullable sessions.provider_id TEXT — multi-provider phase-2 Chunk E
    # / ruling G2-r; one column only, one account per provider). v65 added the
    # model_providers
    # Codex/ChatGPT-subscription OAuth columns (auth_mode + encrypted
    # codex_access_token/codex_refresh_token +
    # codex_account_id/codex_id_token_expires_at/codex_last_refresh +
    # needs_reauth), stored 1:1 on the provider row (plan amendment A1). v64
    # added the model_providers table (admin-configured non-Anthropic model
    # gateways) + idx_model_providers_enabled. v63 was the favorites->active
    # rename (sessions.is_favorite -> is_active + idx_sessions_favorite ->
    # idx_sessions_active). v62 added the persisted SDK context-usage snapshot
    # (additive nullable sessions.context_usage_json JSONB). v61 was the V2
    # Phase-4 placement layer; v60 was the Phase-3 origin collapse; v59 added
    # the Phase-2 turns table; v58 was the Phase-1 deletion (v2_stream forced
    # TRUE + default flipped); v57 added the opt-in flag; v56 added the Phase-0
    # primitives.
    # Parity is the invariant; the floor catches an accidental DOWNgrade
    # without needing an edit here on every additive bump.
    assert db_version == pg_version
    assert db_version >= MIN_SCHEMA_VERSION


def test_column_exists_after_create_postgres_schema(test_db):
    # The conftest fixture builds the schema via create_postgres_schema().
    cols = [r["name"] for r in test_db.execute(
        "SELECT name FROM pragma_table_info('sessions')"
    ).fetchall()]
    assert "parked_task_kind" in cols


def test_column_migration_from_v50_database(mock_db, test_db):
    # Simulate a v50 database (automation_fires landed, no parked_task_kind)
    # and verify the idempotent column migration adds it.
    from tests.pg_shim import ShimCursor
    from app.db.database import _run_column_migrations

    test_db.execute("ALTER TABLE sessions DROP COLUMN parked_task_kind")
    test_db.commit()
    cols = [r["name"] for r in test_db.execute(
        "SELECT name FROM pragma_table_info('sessions')"
    ).fetchall()]
    assert "parked_task_kind" not in cols

    _run_column_migrations(ShimCursor(test_db.cursor()))
    test_db.commit()

    cols = [r["name"] for r in test_db.execute(
        "SELECT name FROM pragma_table_info('sessions')"
    ).fetchall()]
    assert "parked_task_kind" in cols

    # Idempotent: a second run must not fail on the existing column.
    _run_column_migrations(ShimCursor(test_db.cursor()))

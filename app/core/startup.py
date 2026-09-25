"""
Application startup and shutdown orchestration.

Extracts initialization phases from the main lifespan function
into focused, testable functions.
"""

import asyncio
import logging
import sys
import time
from typing import List, Optional

from app.core.config import settings, ensure_directories, load_workspace_from_database
from app.db import database
from app.db.async_database import run_sync
from app.db.database import init_database
from app.core.profiles import run_migrations
from app.core.auth import auth_service
from app.core import encryption

logger = logging.getLogger(__name__)


def _reject_legacy_sqlite() -> None:
    """Never import, rename, or ignore an old database during managed startup."""
    sqlite_path = settings.db_path
    if sqlite_path.exists() or sqlite_path.is_symlink():
        raise RuntimeError(
            "Legacy SQLite data detected. Startup is stopped without modifying it; "
            "explicit offline migration is required."
        )

def _backfill_attachment_data() -> None:
    """Copy local attachment files into the shared database (idempotent).

    Older attachments stored their bytes only on the uploading machine's disk.
    For any attachment whose ``data`` column is still empty, read the local
    cache file (if present on this machine) and store its bytes so installs
    sharing the database can serve it. Attachments whose file lives on another
    machine are skipped here and get picked up when that machine starts.
    """
    from app.core.note_files import read_attachment_bytes

    try:
        missing = database.get_attachments_missing_data()
    except Exception:
        logger.exception("Failed to query attachments needing byte backfill")
        return

    if not missing:
        return

    backfilled = 0
    for att in missing:
        data = read_attachment_bytes(att["file_path"])
        if data is None:
            continue  # File not on this machine; another install may hold it.
        try:
            database.set_note_attachment_data(att["id"], data)
            backfilled += 1
        except Exception:
            logger.exception("Failed to backfill attachment %s into database", att["id"])

    if backfilled:
        logger.info(f"Backfilled {backfilled} note attachment(s) into the shared database")


def _adopt_orphaned_instance_data() -> None:
    """Re-parent projects and sessions orphaned by Docker container ID changes.

    Before the fix, effective_instance_id fell back to socket.gethostname(),
    which in Docker equals the container ID — a random hex string that changes
    on every rebuild. This left data tagged with the old container ID invisible.

    This function finds instance_ids that look like Docker container hex IDs
    (12-char hex strings) and re-assigns them to the current stable instance_id.
    It only touches hex-looking IDs to avoid clobbering data from legitimately
    separate named instances.

    Runs synchronously at boot, before the server accepts traffic, so it stays
    ONE round trip: the two ``SELECT DISTINCT instance_id`` scans are unioned
    into a single statement, and both columns are indexed
    (``idx_projects_instance`` / ``idx_sessions_instance``) so each side is an
    index scan rather than a sequential scan of the table.
    """
    import re
    from app.db.base import get_db

    current_id = settings.effective_instance_id

    # Docker container IDs/hostnames are 12-char lowercase hex strings.
    # Real user-set instance_ids (like "ai-shuffle") won't match this.
    _DOCKER_HEX_RE = re.compile(r'^[0-9a-f]{12}$')

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Candidate orphan instance_ids from BOTH tables in one statement
            # (UNION already deduplicates).
            cursor.execute(
                "SELECT instance_id FROM projects "
                "WHERE instance_id IS NOT NULL AND instance_id != %s "
                "UNION "
                "SELECT instance_id FROM sessions "
                "WHERE instance_id IS NOT NULL AND instance_id != %s",
                (current_id, current_id),
            )
            orphan_ids = []
            for row in cursor.fetchall():
                value = row["instance_id"] if isinstance(row, dict) else row[0]
                if value and _DOCKER_HEX_RE.match(value):
                    orphan_ids.append(value)

            if not orphan_ids:
                return

            # Re-parent orphaned projects
            for oid in orphan_ids:
                cursor.execute(
                    "UPDATE projects SET instance_id = %s WHERE instance_id = %s",
                    (current_id, oid)
                )
                proj_count = cursor.rowcount
                cursor.execute(
                    "UPDATE sessions SET instance_id = %s WHERE instance_id = %s",
                    (current_id, oid)
                )
                sess_count = cursor.rowcount
                if proj_count or sess_count:
                    logger.info(
                        f"Adopted orphaned data from container '{oid}': "
                        f"{proj_count} projects, {sess_count} sessions"
                    )

            conn.commit()
    except Exception as e:
        logger.warning(f"Orphaned instance data adoption failed (non-fatal): {e}")


def init_core() -> None:
    """Phase 1: Initialize core infrastructure (directories, database, encryption)."""
    _reject_legacy_sqlite()
    ensure_directories()

    # Initialize PostgreSQL connection pool
    from app.db.base import init_pool
    init_pool(
        min_conn=settings.db_pool_min,
        max_conn=settings.db_pool_max,
    )

    init_database()
    run_migrations()

    from app.db import mobile_tools, mobile_tool_identity
    mobile_tools.init_schema()
    mobile_tool_identity.init_schema()

    # Adopt orphaned projects/sessions from previous Docker containers.
    # Before this fix, instance_id fell back to socket.gethostname() which
    # returns the Docker container ID — a value that changes on every rebuild.
    # This left projects and sessions invisible after a rebuild.
    _adopt_orphaned_instance_data()

    # Materialize notes as physical markdown files
    from app.core.note_files import materialize_all_notes, get_notes_dir
    notes_dir = get_notes_dir()
    unmaterialized = database.get_notes_without_file_path()
    if unmaterialized:
        logger.info(f"Materializing {len(unmaterialized)} notes as markdown files...")
        path_updates = materialize_all_notes(unmaterialized)
        database.bulk_update_note_file_paths(path_updates)
        logger.info(f"Materialized {len(path_updates)} note files to {notes_dir}")

    # Backfill attachment bytes into the shared database. Attachments uploaded
    # before bytes were stored in the DB only exist on the machine that uploaded
    # them; this pass copies any local file into the DB so installs sharing the
    # database can serve it. Runs on every machine — whichever holds the file
    # wins, and missing files are simply skipped.
    _backfill_attachment_data()

    # Seed built-in subagents
    from app.core.builtin_subagents import seed_builtin_subagents
    seed_results = seed_builtin_subagents()
    created = [k for k, v in seed_results.items() if v == "created"]
    promoted = [k for k, v in seed_results.items() if v == "promoted"]
    if created:
        logger.info(f"Created built-in subagents: {', '.join(created)}")
    if promoted:
        logger.info(f"Promoted to built-in subagents: {', '.join(promoted)}")

    # Retire the chat-history-summarizer builtin (superseded by fluid's
    # session-search tool). Seeding does not prune removed builtins, so
    # drop the stale row explicitly — once; later boots find nothing.
    from app.core.builtin_subagents import get_builtin_subagent_ids
    if "chat-history-summarizer" not in get_builtin_subagent_ids():
        if database.delete_subagent("chat-history-summarizer"):
            logger.info("Removed retired built-in subagent: chat-history-summarizer")


def init_encryption_and_sessions() -> None:
    """Phase 2: Initialize encryption key and manage sessions."""
    if encryption.initialize_server_key(database):
        logger.info("Credential storage initialized from private server key custody")
    else:
        # Keep the local UI available for an honest recovery status. Auth and
        # secret writers must fail closed; sign-in cannot unlock server custody.
        logger.error("Credential storage requires local recovery (%s). Run python -m scripts.credential_keys --help",
                     encryption.credential_key_status()['state'])


def init_workspace() -> None:
    """Phase 3: Configure and verify workspace directory."""
    load_workspace_from_database()
    settings.effective_workspace_dir.mkdir(parents=True, exist_ok=True)


def check_prerequisites() -> None:
    """Phase 4: Verify external dependencies are available."""
    # Ask the CLI itself (`claude auth status`); this also warms the probe
    # cache that /health and the runner capabilities read.
    from app.core import claude_cli_status
    cli = claude_cli_status.get_status(force=True)
    if cli.signed_in:
        logger.info("Claude Code CLI %s: signed in", cli.cli_version or "")
    elif cli.installed:
        logger.warning("Claude Code CLI: not signed in. Sign in from Settings or run `claude auth login` on this worker")
    else:
        logger.warning("Claude Code CLI: %s", cli.error or "not installed")
    from app.core import claude_relogin_notice
    claude_relogin_notice.mark_if_needed()

    # Record the Claude Code auth mode once for an install that has none, so a
    # later change in credentials can never switch it silently.
    from app.core import claude_auth_mode
    try:
        mode = claude_auth_mode.ensure_mode_recorded(cli.signed_in)
        logger.info("Claude Code auth mode: %s", mode)
    except Exception:
        logger.warning("Could not record the Claude Code auth mode", exc_info=True)

    # The one credential gate (license plus the active mode's credential). With
    # neither a CLI sign-in nor a stored key the worker stays in a blocking
    # setup state: every new agent run is refused with the setup message, and
    # nothing falls back to any other credential.
    from app.core import credential_gate
    credential_gate.record_startup(cli)

    if auth_service.is_setup_required():
        logger.info("Local setup required - visit /setup on this computer or your LAN")
    else:
        logger.info("Local installation configured; account sign-in is optional for remote access")

    _check_browser_mcp_prereqs()
    _warn_removed_env_vars()


def _warn_removed_env_vars() -> None:
    """Log names (never values) of removed application environment flags."""
    import os

    for removed in ("SESSION_STORE_MODE",):
        if os.environ.get(removed):
            logger.warning(
                "%s is set but no longer read — the flag was removed. "
                "Remove it from your environment/.env.", removed
            )


def _check_browser_mcp_prereqs() -> None:
    """Log guidance (non-fatally) about the ``browser`` built-in.

    The browser built-in is a spawned STDIO MCP server that attaches to the
    supervised Chromium over CDP (``CHROME_CDP_PORT``, default 9221). What it
    CANNOT do on its own is provide that running browser — so we just remind
    operators that a supervised Chromium must be up. This is a guidance log
    only; it never blocks startup.
    """
    try:
        if not settings.start_chrome:
            logger.info(
                "Browser automation is DISABLED (START_CHROME=0): no Chromium is "
                "launched and sessions do not receive the 'browser' MCP server."
            )
            return
        logger.info(
            "Browser automation is ENABLED (sessions spawn the 'browser' MCP "
            "server, exposing mcp__browser__* tools). Requires a supervised "
            "Chromium reachable over CDP (CHROME_CDP_PORT, default 9221)."
        )

        # Native Chrome is demand-started by browser_runtime.Runtime._connect
        # for both panel and agent use. Startup alone must not create a browser;
        # once started, it stays supervised while sessions may own pages.
    except Exception:
        # Never let a best-effort prereq check break startup.
        logger.debug("Browser MCP prereq check skipped", exc_info=True)


async def start_services() -> None:
    """Phase 5: Start async services (connectors, automations, catalog)."""
    from app.core.global_events import global_events
    global_events.start()
    from app.core.connectors.manager import connector_manager
    await connector_manager.start_all()
    connector_manager.start_event_relay()

    from app.core.automation_manager import automation_manager
    await automation_manager.start()

    from app.services.fal_catalog import catalog_service
    # Reads the whole model catalog; keep it off the loop so boot does not stall
    # the first requests. (Not visible to scan_blocking_db.py — it goes through
    # get_db() inside the service rather than a database.* call.)
    await run_sync(catalog_service.load_from_db)

    # Guaranteed terminal notification (lifecycle batch, item 4b): register the
    # chat layer's callback ONCE so every error/killed transition of a parent's
    # own background work surfaces a durable terminal chip (and, when a live
    # client exists, one consolidated unpark) — even on kill paths with no
    # reader (force-eviction reaps, ghost reaps, pool teardown).
    from app.api.chat_ws_handler import register_terminal_notification_callback
    register_terminal_notification_callback()


def start_background_tasks() -> List[asyncio.Task]:
    """Phase 6: Start periodic background tasks. Returns tasks for shutdown cancellation."""
    tasks = []

    async def _periodic_session_cleanup():
        """Background task: purge expired sessions every hour."""
        while True:
            try:
                await asyncio.sleep(3600)  # 1 hour
                _cleanup_start = time.monotonic()
                await asyncio.to_thread(database.cleanup_expired_sessions)
                _cleanup_elapsed = time.monotonic() - _cleanup_start
                logger.info(f"Periodic session cleanup completed in {_cleanup_elapsed:.2f}s")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Periodic session cleanup failed: {e}")

    tasks.append(asyncio.create_task(_periodic_session_cleanup()))
    logger.info("Periodic session cleanup task started (every 1 hour)")

    async def _periodic_db_maintenance():
        """Background task: database maintenance every 4 hours."""
        from app.core import streaming_status

        # Run once on startup after a short delay (catch up after long uptime).
        await asyncio.sleep(30)
        while True:
            try:
                # Skip VACUUM if any session is actively streaming — VACUUM
                # acquires an exclusive lock that would block all DB reads/writes
                # for potentially several seconds on large databases. Busy truth
                # comes from the durable runs table (lease-valid active runs).
                has_streaming = await asyncio.to_thread(
                    streaming_status.has_any_active_run
                )
                _maint_start = time.monotonic()
                result = await asyncio.to_thread(
                    database.run_database_maintenance,
                    skip_vacuum=has_streaming,
                )
                _maint_elapsed = time.monotonic() - _maint_start
                logger.info(f"Database maintenance completed in {_maint_elapsed:.2f}s: {result}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Database maintenance failed: {e}")
            await asyncio.sleep(4 * 3600)  # 4 hours

    tasks.append(asyncio.create_task(_periodic_db_maintenance()))
    logger.info("Database maintenance task started (every 4 hours, first run in 30s)")

    notes_interval = settings.notes_sync_interval_seconds
    if notes_interval and notes_interval > 0:
        async def _periodic_notes_sync():
            """Background task: reconcile note .md files with the shared DB.

            Notes are materialized to per-install local files for the agent to
            edit; nothing else actively pushes those direct file edits back to
            the shared database (or pulls another install's DB changes down to
            local files). This worker closes that loop in both directions so an
            edit made anywhere — agent, SSH/SMB, or a peer install — converges
            everywhere without anyone opening the note in the UI.
            """
            from app.core import notes_sync

            # Let startup materialization finish first.
            await asyncio.sleep(10)
            while True:
                try:
                    await asyncio.to_thread(notes_sync.sync_notes_once)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Periodic notes sync failed: {e}")
                try:
                    await asyncio.sleep(notes_interval)
                except asyncio.CancelledError:
                    break

        tasks.append(asyncio.create_task(_periodic_notes_sync()))
        logger.info(
            f"Notes disk-sync task started (every {notes_interval}s, first run in 10s)"
        )

    codex_refresh_interval = settings.codex_token_refresh_interval_seconds
    if codex_refresh_interval and codex_refresh_interval > 0:
        async def _periodic_codex_token_refresh():
            """Background task: refresh due Codex/ChatGPT provider OAuth tokens.

            Scans every enabled Codex provider and refreshes any whose access
            token is proactively due (8-day interval) or nearing JWT ``exp``
            (5-min skew). Refreshes are serialized per provider (rotating refresh
            token — a double refresh is permanent death), and permanent failures
            flip ``needs_reauth`` + surface a reconnect prompt. See
            app.core.codex_token_refresh.
            """
            from app.core import codex_token_refresh

            await asyncio.sleep(20)  # let startup settle before the first sweep
            while True:
                try:
                    n = await codex_token_refresh.refresh_due_providers()
                    if n:
                        logger.info(f"Codex token refresh: rotated {n} provider(s)")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Periodic Codex token refresh failed: {e}")
                try:
                    await asyncio.sleep(codex_refresh_interval)
                except asyncio.CancelledError:
                    break

        tasks.append(asyncio.create_task(_periodic_codex_token_refresh()))
        logger.info(
            f"Codex token-refresh task started "
            f"(every {codex_refresh_interval}s, first run in 20s)"
        )

    xai_refresh_interval = settings.xai_token_refresh_interval_seconds
    if xai_refresh_interval and xai_refresh_interval > 0:
        async def _periodic_xai_token_refresh():
            """Background task: refresh due Grok/SuperGrok provider OAuth tokens.

            Scans every enabled Grok-subscription provider and refreshes any
            whose access token is proactively due or nearing JWT ``exp``.
            Permanent failures flip ``needs_reauth`` + surface a reconnect
            prompt. See app.core.xai_token_refresh.
            """
            from app.core import xai_token_refresh

            await asyncio.sleep(20)
            while True:
                try:
                    n = await xai_token_refresh.refresh_due_providers()
                    if n:
                        logger.info(f"Grok token refresh: rotated {n} provider(s)")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Periodic Grok token refresh failed: {e}")
                try:
                    await asyncio.sleep(xai_refresh_interval)
                except asyncio.CancelledError:
                    break

        tasks.append(asyncio.create_task(_periodic_xai_token_refresh()))
        logger.info(
            f"Grok token-refresh task started "
            f"(every {xai_refresh_interval}s, first run in 20s)"
        )

    # Durable busy-status reconciler (v2 core): re-checks sessions last
    # broadcast as busy so a lease that expires with no terminal event (crashed
    # runner) still turns the board spinner off. Also registers the event
    # gateway global listener as the secondary re-check trigger.
    from app.core import streaming_status
    streaming_status.install_gateway_listener()
    tasks.append(asyncio.create_task(streaming_status.sweep_loop()))
    logger.info(
        f"Streaming-status sweep task started "
        f"(every {int(streaming_status.SWEEP_INTERVAL_SECONDS)}s)"
    )

    # Durable wakeups: rehydrate this install's persisted pending wakeups
    # (ScheduleWakeup + idle-watchdog nudges dropped by the restart) and re-arm
    # their timers. Fire-and-forget: claim-on-fire makes a double-arm harmless,
    # so this never needs to block boot.
    from app.core import wakeup_service
    tasks.append(asyncio.create_task(wakeup_service.rehydrate_pending()))
    logger.info("Durable-wakeup rehydrate scheduled")

    # Idle-at-edge watchdog: periodic sweep that nudges sessions sitting
    # idle above the context-window edge so the CLI runs its deferred compaction
    # instead of parking forever. Flag-gated (IDLE_WATCHDOG_ENABLED).
    if settings.idle_watchdog_enabled:
        tasks.append(asyncio.create_task(wakeup_service.watchdog_loop()))
        logger.info(
            "Idle-at-edge watchdog started "
            f"(every {int(settings.idle_watchdog_interval_seconds)}s, "
            f">= {settings.idle_watchdog_context_fraction * 100:.0f}% context)"
        )
    else:
        logger.info("Idle-at-edge watchdog disabled (IDLE_WATCHDOG_ENABLED=false)")

    # License lease renewal (daily, jittered backoff). Idle in beta mode.
    from app.core import license as licensing
    tasks.append(asyncio.create_task(licensing.refresh_loop(), name="license-refresh"))
    logger.info("License mode: %s", "beta" if licensing.beta_mode() else "licensed")

    return tasks


def install_v2_turns_wiring() -> None:
    # V2 turns wiring: install the scheduler seams (kick /
    # on_turn_settled / on_task_terminal) and route turn_manager's interrupt
    # executor to the live SessionRuntime — turn_manager must not import
    # session_runtime, so the routing closure lives here.
    from app.core import session_runtime, turn_manager, turn_scheduler

    turn_scheduler.install()
    from app.core import lifecycle_events
    lifecycle_events.start()

    async def _route_interrupt(
        session_id: str, mode: str, turn_id: Optional[str] = None
    ) -> None:
        runtime = session_runtime.get_live_runtime(session_id)
        if runtime is None:
            return
        # A turn-scoped stop only signals the live runtime when it is
        # actually executing the targeted turn. Belt-and-suspenders behind the
        # DB guard (the durable flip already required id=turn_id, so a mismatch
        # would not have fired the executor); guards the rare window where this
        # process's runtime has already advanced past turn_id. turn_id=None
        # (session-scoped callers) keeps the unconditional signal.
        if turn_id is not None and runtime.turn_id != turn_id:
            return
        await runtime.interrupt(mode)

    turn_manager.register_interrupt_executor(_route_interrupt)
    logger.info("V2 turns wiring installed (scheduler + interrupt executor)")

    # Cross-runner turn-claim loop. Started here (not
    # start_background_tasks) so it is alive BEFORE the boot reconcile sweep:
    # turns recovered on boot for sessions placed on THIS runner are claimed
    # on the first tick instead of waiting out the reconcile's kicks.
    from app.core import turn_claim

    turn_claim.start()

    # Cross-install git-ops claim passes. Poll-only (no NOTIFY wiring — the
    # spawn path's inline claim covers the one latency-critical case); they
    # register on turn_claim's shared sweep driver.
    from app.core import git_ops_runner

    git_ops_runner.start()

    # Pending-interaction expiry sweeper — a ~10s pass on the shared sweep
    # driver that flips lapsed permission/question rows to ``expired`` and wakes
    # their waiters. Belt to the in-process waiter's own local deadline
    # (interaction_wait).
    from app.core import interaction_sweeper

    interaction_sweeper.start()


# Process-wide "we are tearing down" flag. Set FIRST inside shutdown_services
# (before any teardown step runs) and never cleared for the process lifetime.
# Consulted by the lifecycle-notification paths so they degrade gracefully:
#   - unpark delivery (_deliver_unpark_impl / the registry terminal-notification
#     callback) must NOT start a new run_query follow-up turn while the pooled
#     clients are being torn down — the durable terminal chip is persisted
#     instead;
#   - shutdown-origin task reaps stamp "Server restarted — background task
#     stopped" (reason="server_shutdown") so the kill is attributed honestly
#     instead of looking like a user/task failure.
is_shutting_down: bool = False


async def shutdown_services() -> None:
    """Shutdown async services in correct dependency order.

    ORDER MATTERS (lifecycle-notification batch, item 1). The old order closed
    the Postgres pool FIRST, so every later teardown step (reap persistence,
    parked-marker writes) hit a closed pool and its durable writes were lost.
    The pinned order is:

        1. is_shutting_down = True   (gates new unpark follow-ups everywhere)
        2. stop sessions, then pool  (settle turns before disconnecting clients)
        3. in-memory reset_all()s    (live store / permissions / task registry)
        4. remaining services        (http client, automations, connectors)
        5. close_pool — VERY LAST    (every step above may still need the DB)
    """
    global is_shutting_down
    is_shutting_down = True
    from app.core.global_events import global_events
    await global_events.shutdown()
    from app.core import lifecycle_events
    await lifecycle_events.stop()

    # Stop the V2 cross-runner claim loop FIRST (rF4): it kicks the scheduler,
    # and a pass racing the pool shutdown below could start new work
    # mid-teardown. Looked up via sys.modules (not imported) so a flag-OFF
    # process — which never imports the module — stays import-free here too.
    try:
        turn_claim_mod = sys.modules.get("app.core.turn_claim")
        if turn_claim_mod is not None:
            await turn_claim_mod.stop()
            logger.info("V2 turn-claim loop stopped")
    except Exception:
        pass

    # Stop the git-ops claim loop alongside it — a pass racing teardown could
    # otherwise start a git worktree add mid-shutdown. Looked up via sys.modules
    # so a process that never started it stays import-free here too.
    try:
        git_ops_mod = sys.modules.get("app.core.git_ops_runner")
        if git_ops_mod is not None:
            await git_ops_mod.stop()
            logger.info("Git-ops claim loop stopped")
    except Exception:
        pass

    # Stop the pending-interaction sweeper alongside the claim loop.
    # Looked up via sys.modules so a flag-OFF process stays import-free.
    try:
        sweeper_mod = sys.modules.get("app.core.interaction_sweeper")
        if sweeper_mod is not None:
            await sweeper_mod.stop()
            logger.info("V2 interaction sweeper stopped")
    except Exception:
        pass

    # Settle turns and release their leases before disconnecting clients or DB.
    try:
        from app.core.desktop_lifecycle import stop_running_sessions
        await stop_running_sessions()
    except Exception:
        logger.exception("Failed to stop sessions during shutdown")

    # Shutdown the session pool (disconnect all live adapters)
    try:
        from app.core.session_pool import get_session_pool
        await get_session_pool().shutdown()
        logger.info("SDK session pool shut down")
    except Exception:
        pass

    # Free in-memory permission/task state (never otherwise emptied on a
    # long-lived server).
    try:
        from app.core.permission_handler import permission_handler
        from app.core.task_registry import task_registry
        permission_handler.reset_all()
        task_registry.reset_all()
    except Exception:
        pass

    # Stop the native Chrome supervisor (no-op in container / when never started).
    # Looked up via sys.modules so a process that never imported it stays
    # import-free here too.
    try:
        native_chrome_mod = sys.modules.get("app.core.native_chrome")
        if native_chrome_mod is not None:
            await native_chrome_mod.stop()
            logger.info("Native Chrome supervisor stopped")
    except Exception:
        pass

    from app.core.http_client import close_http_client
    await close_http_client()

    # Shutdown automations before connectors, since automations may use them
    from app.core.automation_manager import automation_manager
    await automation_manager.shutdown()

    from app.core.connectors.manager import connector_manager
    await connector_manager.shutdown_all()

    # Close the database connection pool (Postgres) VERY LAST: every teardown
    # step above may still persist durable state (terminal chips, reap
    # attribution, parked markers) and would otherwise die on a closed pool.
    from app.db.base import close_pool
    close_pool()


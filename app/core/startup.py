"""
Application startup and shutdown orchestration.

Extracts initialization phases from the main lifespan function
into focused, testable functions.
"""

import asyncio
import logging
import time
from typing import List

from app.core.config import settings, ensure_directories, load_workspace_from_database
from app.db import database
from app.db.database import init_database
from app.core.profiles import run_migrations
from app.core.auth import auth_service
from app.core import encryption

logger = logging.getLogger(__name__)


def init_core() -> None:
    """Phase 1: Initialize core infrastructure (directories, database, encryption)."""
    ensure_directories()
    init_database()
    run_migrations()

    # Materialize notes as physical markdown files
    from app.core.note_files import materialize_all_notes, get_notes_dir
    notes_dir = get_notes_dir()
    unmaterialized = database.get_notes_without_file_path()
    if unmaterialized:
        logger.info(f"Materializing {len(unmaterialized)} notes as markdown files...")
        path_updates = materialize_all_notes(unmaterialized)
        database.bulk_update_note_file_paths(path_updates)
        logger.info(f"Materialized {len(path_updates)} note files to {notes_dir}")

    # Seed built-in subagents
    from app.core.builtin_subagents import seed_builtin_subagents
    seed_results = seed_builtin_subagents()
    created = [k for k, v in seed_results.items() if v == "created"]
    promoted = [k for k, v in seed_results.items() if v == "promoted"]
    if created:
        logger.info(f"Created built-in subagents: {', '.join(created)}")
    if promoted:
        logger.info(f"Promoted to built-in subagents: {', '.join(promoted)}")


def init_encryption_and_sessions() -> None:
    """Phase 2: Initialize encryption key and manage sessions."""
    if encryption.init_encryption_from_env(database):
        logger.info("Encryption initialized from ADMIN_PASSWORD environment variable")
    else:
        # Clear all sessions on startup - forces re-login to restore encryption key
        # This is required because the encryption key (derived from admin password)
        # is only kept in memory and lost on restart
        admin_cleared, api_cleared = database.clear_all_sessions()
        if admin_cleared or api_cleared:
            logger.info(f"Cleared {admin_cleared} admin and {api_cleared} API user sessions (re-login required)")
        logger.info("Set ADMIN_PASSWORD env var for automatic encryption key loading")


def init_workspace() -> None:
    """Phase 3: Configure and verify workspace directory."""
    load_workspace_from_database()
    settings.effective_workspace_dir.mkdir(parents=True, exist_ok=True)


def check_prerequisites() -> None:
    """Phase 4: Verify external dependencies are available."""
    if auth_service.is_claude_authenticated():
        logger.info("Claude CLI: Authenticated")
    else:
        logger.warning("Claude CLI: Not authenticated - run 'claude login' in container")

    if auth_service.is_setup_required():
        logger.info("Admin setup required - visit /setup to create admin account")
    else:
        logger.info(f"Admin user: {auth_service.get_admin_username()}")


async def start_services() -> None:
    """Phase 5: Start async services (connectors, automations, catalog)."""
    from app.core.connectors.manager import connector_manager
    await connector_manager.start_all()

    from app.core.automation_manager import automation_manager
    await automation_manager.start()

    from app.services.fal_catalog import catalog_service
    catalog_service.load_from_db()


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
        """Background task: SQLite maintenance every 4 hours."""
        from app.core.live_session_store import live_session_store

        # Run once on startup after a short delay (catch up after long uptime).
        await asyncio.sleep(30)
        while True:
            try:
                # Skip VACUUM if any session is actively streaming — VACUUM
                # acquires an exclusive lock that would block all DB reads/writes
                # for potentially several seconds on large databases.
                has_streaming = any(
                    s.is_streaming
                    for s in live_session_store._sessions.values()  # noqa: SLF001
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

    return tasks


async def shutdown_services() -> None:
    """Shutdown async services in correct dependency order."""
    from app.core.http_client import close_http_client
    await close_http_client()

    # Shutdown automations before connectors, since automations may use them
    from app.core.automation_manager import automation_manager
    await automation_manager.shutdown()

    from app.core.connectors.manager import connector_manager
    await connector_manager.shutdown_all()

    # Shutdown SDK session pool (disconnect all live subprocesses)
    try:
        from app.core.session_pool import get_session_pool
        await get_session_pool().shutdown()
        logger.info("SDK session pool shut down")
    except Exception:
        pass

    # Shutdown database connection pool (close all idle connections)
    try:
        from app.db.base import _pool as db_pool
        db_pool.shutdown()
    except Exception:
        pass

"""
Pytest configuration and shared fixtures for AI Shuffle tests.

This module provides:
- In-memory SQLite database for isolation
- FastAPI TestClient for API testing
- Mock auth helpers
- Temporary directories for file operations
"""

import os
import sys
import sqlite3
import tempfile
import pytest
from pathlib import Path
from typing import Generator
from unittest.mock import patch, AsyncMock
from contextlib import contextmanager

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment variables before importing app modules
os.environ["TESTING"] = "1"

# The FastAPI TestClient issues same-origin requests but sends no Origin/Referer
# header and reports a client host of "testclient" (not localhost), so the CSRF
# middleware would reject every state-changing request (POST/PUT/DELETE/PATCH)
# with a 403. Running the main app in wildcard CORS mode puts the CSRF middleware
# into development mode, where it skips these checks — matching how the app is
# exercised locally. This must be set before app.core.config.settings is first
# imported (settings reads CORS_ORIGINS from the environment at instantiation).
# The dedicated CSRF middleware tests build their own app with an explicit
# non-wildcard origin, so they are unaffected by this default. This is forced
# (not setdefault) because the host environment may export a real CORS_ORIGINS
# for the live deployment, which must not leak into the test process.
os.environ["CORS_ORIGINS"] = "*"

# =============================================================================
# SAFETY: forbid opening a real Postgres pool during tests
# =============================================================================
# The application is Postgres-only and the configured POSTGRES_* settings
# frequently point at a real/shared/production database. Multiple code paths will open a pool against
# it if allowed to:
#   * app.db.base.init_pool() — invoked by the FastAPI lifespan (entered by the
#     TestClient fixtures) AND directly by the PgSessionStore conformance
#     fixtures in tests/core/test_pg_session_store*.py. That conformance harness
#     TRUNCATES the session_store_* tables between contracts.
#   * Once any real pool exists, the per-test _auto_db_shim fixture stands aside
#     (see below), so the *entire* remaining suite silently runs — and mutates —
#     that database.
# The per-test shim cannot fully prevent this: module-scoped fixtures are set up
# before function-scoped ones, so init_pool() runs before the shim's no-op patch
# is active. We therefore neutralise pool creation at conftest *import* time —
# the earliest possible point, before any collection or fixture — and keep
# _pg_pool None so the in-memory SQLite shim is always used.
#
# Opt in to a real Postgres pool ONLY against a disposable test database by
# exporting ALLOW_REAL_PG=1.
ALLOW_REAL_PG = os.environ.get("ALLOW_REAL_PG") == "1"
if not ALLOW_REAL_PG:
    from app.db import base as _db_base

    def _blocked_init_pool(*args, **kwargs):
        raise RuntimeError(
            "init_pool() is disabled in the test suite to prevent connecting to "
            "the configured Postgres database (which may be production). Tests "
            "run on the in-memory SQLite shim. Set ALLOW_REAL_PG=1 to override, "
            "and only point the POSTGRES_* settings at a disposable test "
            "database when you do."
        )

    _db_base._pg_pool = None
    _db_base.init_pool = _blocked_init_pool
    _db_base.close_pool = lambda *args, **kwargs: None


# =============================================================================
# Database Fixtures
# =============================================================================

@pytest.fixture(scope="function")
def _raw_sqlite() -> Generator[sqlite3.Connection, None, None]:
    """One in-memory SQLite connection per test, with the app schema built.

    The schema is generated from the canonical
    ``app.db.schema_postgres.create_postgres_schema`` (run through the
    dialect-translating :class:`~tests.pg_shim.ShimCursor`), so the test
    database always matches production and never drifts. See ``tests/pg_shim.py``.
    """
    from tests.pg_shim import new_sqlite_connection, close_connection

    conn = new_sqlite_connection()
    yield conn

    # issue #622: the event_gateway LISTEN daemon can call record_error ->
    # pg_shim.execute against this shared connection from its own thread. Stop
    # and join that thread BEFORE closing the connection so no cross-thread
    # statement races the close (which would segfault the interpreter, exit 139).
    try:
        from app.core.event_gateway import get_gateway

        get_gateway().stop()
    except Exception:
        pass

    # Close through the shim's race-proof path: it marks the connection closed
    # under the same lock every serialized statement holds, so any late
    # cross-thread statement raises cleanly instead of touching a freed handle.
    close_connection(conn)


@pytest.fixture(scope="function")
def test_db(_raw_sqlite: sqlite3.Connection) -> sqlite3.Connection:
    """The raw SQLite connection backing a test (for direct SQL setup/asserts)."""
    return _raw_sqlite


@pytest.fixture(scope="function", autouse=True)
def _auto_db_shim(_raw_sqlite: sqlite3.Connection, request):
    """Route the app's DB layer to the per-test SQLite connection.

    The application is PostgreSQL-only and every domain module funnels through
    ``app.db.base.get_connection`` (resolved by name inside ``get_db`` /
    ``get_cursor`` / ``transaction``) and ``app.db.database.get_connection``
    (the one module that imports it directly). Patching those two names
    redirects the *entire* DB layer to a :class:`~tests.pg_shim.ShimConnection`
    wrapping the test's SQLite database — no per-module patching needed.

    The shim stands aside ONLY when real Postgres has been explicitly opted into
    (``ALLOW_REAL_PG=1``) and a pool is actually initialised — that is the sole
    mode in which touching a real database is sanctioned. In the default (safe)
    mode ``init_pool`` is blocked at import time and ``_pg_pool`` is kept None,
    so the shim always routes here and no test can reach the configured
    Postgres database. See the safety guard at the top of this file.
    """
    from app.db import base, database
    from tests.pg_shim import ShimConnection

    if ALLOW_REAL_PG and getattr(base, "_pg_pool", None) is not None:
        yield
        return

    shim = ShimConnection(_raw_sqlite)

    def _get_connection():
        return shim

    def _noop(*args, **kwargs):
        # SAFETY: neutralise real-pool init. The FastAPI lifespan (entered by
        # the TestClient-based fixtures) calls init_pool() with the process
        # POSTGRES_* settings — which in a dev/CI sandbox point at the *live*
        # database. Without this, entering TestClient would open a real
        # connection pool to production. Tests run entirely on the SQLite shim.
        return None

    with patch.object(base, "get_connection", _get_connection), \
            patch.object(database, "get_connection", _get_connection), \
            patch.object(base, "init_pool", _noop), \
            patch.object(base, "close_pool", _noop):
        # Seed a "default" profile so tests can reference it.
        try:
            database.create_profile(
                profile_id="default",
                name="Default Profile",
                description=None,
                config={"model": "claude-sonnet-4-20250514"},
            )
        except Exception:
            pass  # May already exist
        yield


@pytest.fixture(scope="function")
def mock_db(_raw_sqlite: sqlite3.Connection, _auto_db_shim) -> sqlite3.Connection:
    """The test SQLite connection, with the app DB layer already routed to it.

    Depending on ``_auto_db_shim`` (autouse) documents the coupling; the patch
    is active for the whole test regardless.
    """
    return _raw_sqlite


# =============================================================================
# FastAPI Test Client
# =============================================================================

@pytest.fixture(scope="function")
def client(mock_db):
    """
    Create a FastAPI TestClient with mocked database.

    Usage:
        def test_endpoint(client):
            response = client.get("/api/v1/health")
            assert response.status_code == 200
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.automation_manager import automation_manager
    from app.core.connectors.manager import connector_manager

    # Prevent scheduler shutdown errors during test teardown
    automation_manager._scheduler = None
    automation_manager._initialized = False

    with TestClient(app) as test_client:
        yield test_client


def _mock_require_auth():
    """Mock require_auth dependency that returns a valid session token."""
    return "test-session-token"


def _mock_require_admin():
    """Mock require_admin dependency that returns None (passes admin check)."""
    return None


@pytest.fixture(scope="function")
def authenticated_client(mock_db):
    """
    Create an authenticated test client with FastAPI dependency overrides.

    This properly overrides the authentication dependencies at the FastAPI level
    so that all protected endpoints accept requests without real authentication.

    Usage:
        def test_protected_endpoint(authenticated_client):
            response = authenticated_client.get("/api/v1/sessions")
            assert response.status_code == 200
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.api.auth import require_auth, require_admin
    from app.core.automation_manager import automation_manager
    from app.core.connectors.manager import connector_manager

    # Prevent scheduler shutdown errors during test teardown
    automation_manager._scheduler = None
    automation_manager._initialized = False

    # Override the dependencies at the FastAPI app level
    app.dependency_overrides[require_auth] = _mock_require_auth
    app.dependency_overrides[require_admin] = _mock_require_admin

    with TestClient(app) as test_client:
        yield test_client

    # Clean up the overrides after the test
    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(require_admin, None)


# =============================================================================
# Auth Fixtures
# =============================================================================

@pytest.fixture(scope="function")
def mock_auth():
    """
    Mock authentication for protected endpoints using patches.

    Note: For HTTP tests with authenticated_client, use the authenticated_client
    fixture which properly overrides FastAPI dependencies. This fixture is for
    direct function calls that need mocked auth.

    This fixture patches:
    - require_auth: Returns a valid session token
    - require_admin: Passes admin check
    - require_api_key: Returns valid API user
    """
    with patch("app.api.auth.require_auth") as mock_require_auth:
        mock_require_auth.return_value = "test-session-token"

        with patch("app.api.auth.require_admin") as mock_require_admin:
            mock_require_admin.return_value = None

            with patch("app.api.auth.get_api_user_from_request") as mock_api_user:
                mock_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user",
                    "role": "admin"
                }

                yield {
                    "require_auth": mock_require_auth,
                    "require_admin": mock_require_admin,
                    "api_user": mock_api_user
                }


@pytest.fixture(scope="function")
def admin_user():
    """
    Return admin user data for tests.
    """
    return {
        "id": "admin-user-id",
        "username": "admin",
        "role": "admin",
        "created_at": "2024-01-01T00:00:00Z"
    }


@pytest.fixture(scope="function")
def api_user():
    """
    Return API user data for tests.
    """
    return {
        "id": "api-user-id",
        "username": "api-user",
        "role": "user",
        "created_at": "2024-01-01T00:00:00Z"
    }


# =============================================================================
# Temporary Directory Fixtures
# =============================================================================

@pytest.fixture(scope="function")
def temp_dir() -> Generator[Path, None, None]:
    """
    Create a temporary directory for file operations.

    Automatically cleaned up after the test.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(scope="function")
def mock_workspace(temp_dir: Path):
    """
    Create a mock workspace directory structure.
    """
    workspace = temp_dir / "workspace"
    workspace.mkdir()

    # Create .claude directory
    claude_dir = workspace / ".claude"
    claude_dir.mkdir()

    return workspace


# =============================================================================
# Sample Data Fixtures
# =============================================================================

@pytest.fixture
def sample_session():
    """
    Return sample session data.
    """
    return {
        "id": "test-session-id",
        "title": "Test Session",
        "profile_id": "default",
        "project_id": None,
        "status": "active",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z"
    }


@pytest.fixture
def sample_profile():
    """
    Return sample profile data.
    """
    return {
        "id": "test-profile-id",
        "name": "Test Profile",
        "system_prompt": "You are a helpful assistant.",
        "model": "claude-sonnet-4-20250514",
        "created_at": "2024-01-01T00:00:00Z"
    }


@pytest.fixture
def sample_project():
    """
    Return sample project data.
    """
    return {
        "id": "test-project-id",
        "name": "Test Project",
        "path": "/path/to/project",
        "created_at": "2024-01-01T00:00:00Z"
    }


# =============================================================================
# Claude SDK Mocks
# =============================================================================

@pytest.fixture
def mock_claude_sdk():
    """
    Mock the Claude Agent SDK for testing query execution.
    """
    with patch("claude_agent_sdk.query") as mock_query:
        # Create an async generator that yields mock messages
        async def mock_query_gen(*args, **kwargs):
            yield {"type": "text", "content": "Mock response"}

        mock_query.return_value = mock_query_gen()
        yield mock_query


# =============================================================================
# Environment Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def reset_environment():
    """
    Reset environment variables after each test.
    """
    original_env = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture(autouse=True)
def reset_shutdown_flag():
    """Clear ``startup.is_shutting_down`` before each test.

    The flag is process-lifetime by design (set in ``shutdown_services`` and
    never cleared — a real process exits). But every TestClient-based fixture
    enters the FastAPI lifespan, whose teardown runs ``shutdown_services`` and
    latches the flag — poisoning every LATER test's unpark-delivery /
    kill-attribution paths (they would all read "shutting down"). Reset BEFORE
    each test so leakage from a prior test's lifespan teardown never crosses.
    """
    try:
        import app.core.startup as _startup
        _startup.is_shutting_down = False
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def reset_worktree_fetch_cache():
    """Clear the cross-device rebind fetch de-dup cache between tests.

    ``worktree_manager`` caches recent ``git fetch`` calls (keyed by clone dir) so
    a fetch primed at adopt time isn't repeated on the send hot path. That module
    -level state would otherwise leak across tests — e.g. a fetch primed for a
    fixed path like ``/local/clone`` in one test would make a later test's
    ``fetch.assert_called_once()`` see zero calls. Reset before each test.
    """
    try:
        from app.core.worktree_manager import reset_fetch_cache
        reset_fetch_cache()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def prevent_real_cli_tool_discovery():
    """
    Guard against tests launching the real ``claude`` CLI for tool discovery.

    ``app.api.system.discover_tools_from_cli`` shells out to
    ``claude --tools default -p exit``, which boots *every* configured MCP server
    (pencil, Gmail, Drive, computer-use, browser, ...). Running that during the
    test suite — especially across multiple parallel sessions — can exhaust
    system memory and freeze the machine.

    Default every test to the deterministic built-in fallback (``tools=None``,
    which makes ``list_available_tools`` use ``BUILTIN_TOOLS``). Tests that need
    discovered-tool behaviour patch ``discover_tools_from_cli`` themselves
    (e.g. tests/api/test_mcp.py); that inner patch transparently overrides this
    guard.
    """
    try:
        import app.api.system as system_mod
    except Exception:
        # If the system module can't be imported, there's nothing to guard.
        yield
        return

    # Discard anything a real discovery call may have cached before the guard.
    system_mod._tools_cache = {"tools": None, "mcp_servers": None, "loaded": False}

    with patch.object(
        system_mod,
        "discover_tools_from_cli",
        new=AsyncMock(return_value={"tools": None, "mcp_servers": None}),
    ):
        yield


@pytest.fixture(autouse=True)
def _idempotent_scheduler_shutdown():
    """Make APScheduler's ``shutdown()`` idempotent for the whole test suite.

    ``automation_manager`` is a process-wide singleton whose ``AsyncIOScheduler``
    is started and stopped by the app lifespan. Every ``TestClient(app)`` context
    (including the many inline ones in tests that don't use the ``client``
    fixture) runs that lifespan, so a second context — or a second lifespan in a
    single test — calls ``shutdown()`` on an already-stopped scheduler, which
    raises ``SchedulerNotRunningError`` from the ``with TestClient(...)`` __exit__
    and fails the test even though its assertions passed. Swallow only that
    specific already-stopped signal so the context manager exits cleanly; real
    shutdown behaviour is otherwise unchanged.
    """
    from apscheduler.schedulers import SchedulerNotRunningError
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    real_shutdown = AsyncIOScheduler.shutdown

    def safe_shutdown(self, *args, **kwargs):
        try:
            return real_shutdown(self, *args, **kwargs)
        except SchedulerNotRunningError:
            return None

    with patch.object(AsyncIOScheduler, "shutdown", safe_shutdown):
        yield


@pytest.fixture(autouse=True)
def reset_global_stores():
    """
    Reset process-wide singleton stores after every test.

    ``task_registry``, ``permission_handler`` and the ``session_pool`` are
    module-level singletons. Any test that drives a real app endpoint populates
    them with per-session state, and nothing empties them between tests. Across
    the full suite this grows monotonically, ballooning RSS until the machine
    freezes. Clear them in teardown so each test starts clean.
    """
    yield
    try:
        from app.core.task_registry import task_registry
        task_registry.reset_all()
    except Exception:
        pass
    try:
        # Durable task-snapshot writer (Phase 1): drop queued snapshots and the
        # consumer task so nothing leaks across per-test event loops.
        from app.core import task_persistence
        task_persistence.reset()
    except Exception:
        pass
    try:
        from app.core.permission_handler import permission_handler
        permission_handler.reset_all()
    except Exception:
        pass
    try:
        import app.core.session_pool as session_pool_mod
        # Drop the pooled SDK clients; a fresh pool is lazily created on next use.
        session_pool_mod._session_pool = None
    except Exception:
        pass


@pytest.fixture
def mock_encryption():
    """
    Mock encryption/decryption for credential tests.
    """
    with patch("app.core.encryption.encrypt_value") as mock_encrypt:
        with patch("app.core.encryption.decrypt_value") as mock_decrypt:
            with patch("app.core.encryption.is_encryption_ready") as mock_ready:
                mock_ready.return_value = True
                mock_encrypt.side_effect = lambda x: f"encrypted:{x}"
                mock_decrypt.side_effect = lambda x: x.replace("encrypted:", "")

                yield {
                    "encrypt": mock_encrypt,
                    "decrypt": mock_decrypt,
                    "is_ready": mock_ready
                }

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

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment variables before importing app modules.
os.environ["TESTING"] = "1"
# Never read a descriptor from the user's live DATA_DIR during collection or
# fixture setup. Disposable PostgreSQL jobs opt in below with TEST_POSTGRES_DSN.
_TEST_DATA = tempfile.TemporaryDirectory(prefix="shuffle-tests-")
os.environ["DATA_DIR"] = _TEST_DATA.name

REPO_ROOT = Path(__file__).resolve().parent.parent

# ─── No real ``claude`` CLI in tests ────────────────────────────────────────
# A test that forgets to point the delegated leg at a fake would otherwise run
# the machine's real ``claude`` binary, which spends the developer's Claude
# subscription and executes model-chosen commands. On Windows this happened
# silently: ``shutil.which`` skips an extension-less shim, so the search walked
# on to ``~/.local/bin/claude.exe``. The session guard below removes every PATH
# directory that holds a ``claude`` binary, except directories inside the repo
# or the system temp dir (where tests install their own fakes). A forgotten
# patch then fails with ClaudeCliMissingError instead of billing anyone.
# Set AI_SHUFFLE_ALLOW_REAL_CLAUDE=1 to keep PATH as it is.

_CLAUDE_BINARY_NAMES = ("claude", "claude.exe", "claude.cmd", "claude.bat")


def _path_forms(path: str) -> set:
    forms = set()
    for form in (os.path.abspath(path), os.path.realpath(path)):
        forms.add(os.path.normcase(form))
    return forms


def _is_inside(path: str, roots) -> bool:
    for form in _path_forms(path):
        for root in roots:
            for root_form in _path_forms(str(root)):
                try:
                    if os.path.commonpath([form, root_form]) == root_form:
                        return True
                except ValueError:  # different drives on Windows
                    continue
    return False


def strip_real_claude_dirs(path_value: str, allowed_roots) -> str:
    """``path_value`` without the entries that hold a ``claude`` binary.

    Entries under one of ``allowed_roots`` are kept, as is every entry with no
    ``claude``, ``claude.exe``, ``claude.cmd`` or ``claude.bat`` file in it.
    """
    kept = []
    for entry in path_value.split(os.pathsep):
        if entry and not _is_inside(entry, allowed_roots) and any(
            os.path.isfile(os.path.join(entry, name)) for name in _CLAUDE_BINARY_NAMES
        ):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


@pytest.fixture(autouse=True, scope="session")
def _no_real_claude_on_path():
    """Keep the machine's real ``claude`` CLI off PATH for the whole run."""
    if os.environ.get("AI_SHUFFLE_ALLOW_REAL_CLAUDE") == "1":
        yield
        return
    roots = (REPO_ROOT, tempfile.gettempdir())
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", strip_real_claude_dirs(os.environ.get("PATH", ""), roots))
        yield


@pytest.fixture(autouse=True)
def _no_real_claude_cli_probe(monkeypatch):
    """Status endpoints must not run whatever `claude` the test machine has.

    The probe finds no CLI unless a test installs its own resolver or stubs
    ``claude_cli_status.probe``; the cache starts empty in every test.
    """
    from app.core import claude_cli_status

    def _missing():
        raise RuntimeError("The Claude Code CLI is not installed (test default).")

    monkeypatch.setattr(claude_cli_status, "_default_resolver", _missing)
    claude_cli_status.invalidate()
    yield
    claude_cli_status.invalidate()


@pytest.fixture(autouse=True)
def _agent_credentials_ready_by_default(request, monkeypatch):
    """Turn-start tests run on a worker that is set up to start agents.

    The test machine has no signed-in CLI (see above) and no stored key, so the
    credential gate would refuse every turn. Tests of the gate itself opt out
    with ``@pytest.mark.real_agent_gate``.
    """
    # Each test starts from a fresh database, so no memoized setting (the
    # Claude Code mode, the loop guard's limits) may carry over from the last.
    from app.db.settings import invalidate_system_setting_memo
    invalidate_system_setting_memo()
    # Nor the default-model resolver's cached usage ranking.
    from app.core.credential_gate import invalidate_most_used_cache
    invalidate_most_used_cache()
    if request.node.get_closest_marker("real_agent_gate"):
        yield
        return
    from app.core import credential_gate

    monkeypatch.setattr(credential_gate, "claude_credential_ready", lambda cli=None: (True, None))
    yield


@pytest.fixture(autouse=True)
def _test_only_memory_store(monkeypatch):
    from app.core.provider.harness_adapter import HarnessAdapter, MemoryStore
    monkeypatch.setattr(HarnessAdapter, "_test_memory_store_factory", MemoryStore)


@pytest.fixture(autouse=True)
def _isolated_computer_use_halt_state(monkeypatch):
    """Stop tests must not leak arbitrary session flags into another test."""
    from app.mcp.computer_use import abort
    with abort._LOCK:
        monkeypatch.setattr(abort, "_HALTED", {})
        monkeypatch.setattr(abort, "_GENERATIONS", {})

@pytest.fixture(autouse=True)
def _no_builtin_skills(tmp_path_factory, monkeypatch):
    """Keep fluid's built-in skills (``fluid/builtin_skills``) out of the skill
    and slash-command listings tests assert on. Tests of a built-in skill
    point ``builtin_skills_dir`` back at the package folder themselves."""
    empty = tmp_path_factory.mktemp("no-builtin-skills")
    monkeypatch.setattr("fluid.context.instructions.builtin_skills_dir", lambda: empty)


@pytest.fixture(autouse=True)
def _isolated_connector_package_store(tmp_path, monkeypatch):
    from app.core import connector_packages
    monkeypatch.setattr(connector_packages, "root", lambda: tmp_path / "connector-packages")


@pytest.fixture
def bundled_connector_catalog(monkeypatch):
    """Legacy service tests do not test plugin installation; package tests do."""
    from app.core import connector_packages
    monkeypatch.setattr(connector_packages, "require_installed", connector_packages.package_id)


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
# The application is Postgres-only and a live managed descriptor may point
# at production data. Multiple code paths will open a pool against
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
# exporting ALLOW_REAL_PG=1 and TEST_POSTGRES_DSN for an explicit disposable DB.
ALLOW_REAL_PG = os.environ.get("ALLOW_REAL_PG") == "1"
if ALLOW_REAL_PG:
    from app.core.config import settings as _test_settings
    from app.db.base import parse_database_url as _parse_test_url
    _test_dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not _test_dsn:
        raise RuntimeError("ALLOW_REAL_PG requires an explicit disposable TEST_POSTGRES_DSN")
    _test_connection = _parse_test_url(_test_dsn)
    if _test_connection["host"] not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("TEST_POSTGRES_DSN must identify a disposable loopback database")
    _test_connection.update(hostaddr="127.0.0.1", options="", connect_timeout=5, sslmode="disable")
    # Instance-only injection is outside application runtime configuration.
    object.__setattr__(_test_settings, "get_database_connection", lambda: dict(_test_connection))
if not ALLOW_REAL_PG:
    from app.db import base as _db_base

    def _blocked_init_pool(*args, **kwargs):
        raise RuntimeError(
            "init_pool() is disabled in the test suite to prevent connecting to "
            "the configured Postgres database (which may be production). Tests "
            "run on the in-memory SQLite shim. Set ALLOW_REAL_PG=1 to override, "
            "with TEST_POSTGRES_DSN pointing at a disposable loopback test database."
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
        # Startup-owned extension schemas are not part of create_postgres_schema.
        from app.db import mobile_tool_identity
        mobile_tool_identity.init_schema()
        # Seed a "default" profile so tests can reference it (unless the test
        # asked for an unseeded database via ``bare_db``).
        if "bare_db" in request.fixturenames:
            yield
            return
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


@pytest.fixture(scope="function")
def bare_db(_raw_sqlite: sqlite3.Connection, _auto_db_shim) -> sqlite3.Connection:
    """Like ``mock_db`` but without the seeded "default" profile.

    For tests that count or list every row of a table (``get_all_profiles``
    and similar) and so must start from an empty database.
    """
    return _raw_sqlite


# =============================================================================
# FastAPI Test Client
# =============================================================================

@pytest.fixture(scope="function")
def client(mock_db, _api_unit_credentials):
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
def authenticated_client(mock_db, _api_unit_credentials):
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


# =============================================================================
# Environment Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def _isolate_test_git_discovery(monkeypatch, tmp_path):
    """A temp folder is never a checkout of the real repo above --basetemp.

    Tests create their own repositories beneath this boundary when needed.
    Preserve any caller-supplied ceilings as well.
    """
    ceilings = [os.environ.get('GIT_CEILING_DIRECTORIES', ''), str(tmp_path.parent)]
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', os.pathsep.join(filter(None, ceilings)))


@pytest.fixture(autouse=True)
def _isolate_credential_state(monkeypatch, tmp_path):
    """Keep per-database process keys and durable files out of other tests/users.

    Custody-specific tests still call the real initializer and can override this
    private location. Legacy/rekey tests start without an adopted-key marker.
    """
    from types import SimpleNamespace
    from app.core import encryption, credential_keys

    monkeypatch.setattr(encryption, '_encryption_key', None)
    monkeypatch.setattr(encryption, '_key_salt', None)
    monkeypatch.setattr(encryption, '_credential_status', {'ready': False, 'state': 'unavailable'})
    monkeypatch.setattr(credential_keys, 'settings', SimpleNamespace(effective_data_dir=tmp_path / 'credential-data'))


@pytest.fixture
def _api_unit_credentials(monkeypatch, _isolate_credential_state):
    """Ordinary API tests exercise real crypto without invoking OS key custody.

    These clients override authentication and use a per-test SQLite database;
    initialize only an ephemeral test key, not durable migration metadata or
    the developer's provider-file seed. Dedicated custody/startup tests and the
    actual first-run integration tests use the real startup path separately.
    Keeping legacy helpers real lets rekey fixtures choose their own test key.
    """
    from app.core import encryption, startup

    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    monkeypatch.setattr(encryption, '_credential_status', {'ready': True, 'state': 'ready'})
    monkeypatch.setattr(startup, 'init_encryption_and_sessions', lambda: None)


@pytest.fixture(autouse=True)
def _no_gh_in_owner_detection(monkeypatch):
    """Assistant owner detection never shells out to the developer's gh.

    The manager loop and GET /assistant?detect=true start a detection pass;
    tests that exercise the GitHub login patch this back themselves.
    """
    from app.core import assistant_introduction

    monkeypatch.setattr(assistant_introduction, "github_login", lambda unknown=None: None)


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
def isolate_fluid_skills_dir(monkeypatch, tmp_path):
    """Keep fluid's skill store out of the real user data directory.

    Code-mode + the continual harness are ON by default, so every native
    HarnessAdapter.connect() builds a ``SkillStore`` — which mkdirs its root and
    appends to ``journal.jsonl`` immediately. Unpinned, that root resolves to
    ``settings.effective_data_dir/fluid/skills`` (i.e. the live ~/.ai-shuffle),
    so merely running tests would pollute real user data. Point it at tmp_path;
    monkeypatch restores the original value after each test.

    Prose skills replicate from ``~/.fluid/skills``; they follow the pinned
    root to a sibling folder, so the network harness gets one per worker and
    no test publishes (or deletes) the developer's real skills.
    """
    from pathlib import Path
    from app.core import fluid_memory
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_skills_dir", str(tmp_path / "fluid-skills"))
    monkeypatch.setattr(fluid_memory, "prose_skills_root",
                        lambda: Path(settings.fluid_skills_dir).parent / "prose-skills")
    yield


@pytest.fixture(autouse=True)
def isolate_fluid_projects_dir(monkeypatch, tmp_path):
    """Keep the native-leg project-memory root out of the real ~/.fluid.

    Ruling MEM1 puts a native session's memory under
    ``<fluid_projects_dir>/<slug>/memory``, defaulting to ~/.fluid/projects.
    Anything that builds options or reconciles memory would otherwise resolve
    (and, on reconcile, create) a dir in the developer's own home. Point it at
    tmp_path; monkeypatch restores the original value after each test.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_projects_dir", str(tmp_path / "fluid-projects"))
    yield


@pytest.fixture(autouse=True)
def live_hub_verdicts(monkeypatch):
    """Ask the hub on every check unless a test opts into the verdict cache.

    ``account_hub.require_active`` remembers positive hub verdicts for a TTL
    and honors them through hub outages. Existing tests flip the mocked hub
    answer mid-test and expect the next request to see it, so the cache and
    grace are zeroed here and the memory is cleared between tests. Cache
    tests set the two settings themselves.
    """
    try:
        from app.core import account_hub
        from app.core.config import settings
        account_hub.reset_verification_cache()
        monkeypatch.setattr(settings, "mobile_account_hub_verify_ttl_seconds", 0.0)
        monkeypatch.setattr(settings, "mobile_account_hub_grace_seconds", 0.0)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _no_host_memory_admission_floor(monkeypatch):
    """Admission must not depend on how much RAM the test host has free.

    ``SessionPool.admission_check`` reads real host MemAvailable against
    ``sdk_admission_min_available_mb``; under ``-n 4`` on a small machine the
    other workers push it below the floor and real turns get refused. Tests
    that exercise the floor set their own value.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "sdk_admission_min_available_mb", 0)


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
def prevent_real_mcp_tool_discovery():
    """
    Guard against tests spawning real MCP servers for tool discovery.

    ``app.core.tool_catalog`` enumerates every configured MCP server by actually
    connecting to it and calling ``tools/list`` (pencil, Gmail, Drive, browser,
    ...). Running that during the test suite — especially across parallel
    sessions — can exhaust system memory and freeze the machine.

    Default every test to "no MCP server answers": the in-process groups
    (harness / app / ai / computer_use / app_config) are still built for real,
    which is what the surface tests assert on. Tests that need a specific MCP
    catalog patch ``_list_mcp_tools`` themselves; the inner patch transparently
    overrides this guard.

    (This replaced a guard against ``app.api.system.discover_tools_from_cli``,
    which ruling T1 deleted along with the rest of the CLI-shelling inventory.)
    """
    try:
        import app.core.tool_catalog as catalog_mod
    except Exception:
        # If the module can't be imported, there's nothing to guard.
        yield
        return

    catalog_mod.clear_mcp_cache()
    with patch.object(
        catalog_mod, "_list_mcp_tools", new=AsyncMock(return_value=[])
    ):
        yield
    catalog_mod.clear_mcp_cache()


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


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    """Point ``artifact_store.ARTIFACTS_DIR`` at a per-test directory."""
    from app.core import artifact_store

    d = tmp_path / "artifacts"
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", d)
    return d


@pytest.fixture
def loop_thread():
    """The thread the test's event loop runs on — every DB call must land
    somewhere else (used by the ``*_offload`` tests)."""
    import threading

    return threading.get_ident()


# ---------------------------------------------------------------------------
# turn_manager async twins
# ---------------------------------------------------------------------------

def async_twin(sync_double):
    """Adapt a sync test double to stand in for a ``turn_manager`` async twin.

    ``turn_manager`` exposes ``submit``/``submit_async`` and
    ``interrupt``/``interrupt_async``. The async twins are not
    ``to_thread(submit)`` — they offload only the DB half and fire the
    scheduler kick / interrupt executor back on the caller's loop, because
    those need a running loop and are silently dropped from a worker thread.

    Production async callers therefore go through the ``*_async`` name, so a
    test that patches only the sync name stops capturing entirely. Pair every
    sync patch with this wrapper so both seams record into the same double and
    existing ``call_args`` assertions keep working.
    """
    async def _twin(*args, **kwargs):
        return sync_double(*args, **kwargs)

    return _twin


# ─── One ``pytest -n`` run per machine ─────────────────────────────────────
# Parallel runs from several agent sessions have saturated the shared worker
# host. The xdist controller waits for a machine-wide file lock before its
# workers start; see tests/_xdist_gate.py. Off on CI, without -n, and with
# AI_SHUFFLE_PYTEST_GATE=0.

def pytest_configure(config):
    from tests import _xdist_gate

    _xdist_gate.configure(config)


def pytest_unconfigure(config):
    from tests import _xdist_gate

    _xdist_gate.unconfigure(config)

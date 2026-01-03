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
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment variables before importing app modules
os.environ["TESTING"] = "1"


# =============================================================================
# Database Fixtures
# =============================================================================

def create_test_schema(cursor: sqlite3.Cursor):
    """Create all database tables for testing."""

    # Schema version table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
    """)

    # System settings
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Admin user
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY DEFAULT 1,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (id = 1)
        )
    """)

    # Agent profiles (legacy table name)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'claude-sonnet-4-20250514',
            system_prompt TEXT,
            allowed_tools TEXT,
            mcp_servers TEXT,
            max_tokens INTEGER DEFAULT 8096,
            permission_mode TEXT DEFAULT 'default',
            custom_instructions TEXT,
            config_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Profiles (current table name used by the app)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            config JSON NOT NULL DEFAULT '{}',
            mcp_tools JSON DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Projects
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            description TEXT,
            git_url TEXT,
            default_branch TEXT DEFAULT 'main',
            profile_id TEXT REFERENCES agent_profiles(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Git repositories
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS git_repositories (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            remote_url TEXT,
            default_branch TEXT DEFAULT 'main',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Worktrees (for parallel session work on branches)
    # Note: session_id is deprecated - relationship is now tracked via sessions.worktree_id
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS worktrees (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            session_id TEXT,
            branch_name TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_branch TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (repository_id) REFERENCES git_repositories(id) ON DELETE CASCADE
        )
    """)

    # Worktree indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_repository ON worktrees(repository_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_session ON worktrees(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_branch ON worktrees(branch_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_status ON worktrees(status)")

    # Sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            profile_id TEXT REFERENCES agent_profiles(id) ON DELETE SET NULL,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            worktree_id TEXT REFERENCES worktrees(id) ON DELETE SET NULL,
            status TEXT DEFAULT 'active',
            is_pinned INTEGER DEFAULT 0,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Auth sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id TEXT PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ip_address TEXT,
            user_agent TEXT
        )
    """)

    # Two-factor auth
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS two_factor_auth (
            id INTEGER PRIMARY KEY DEFAULT 1,
            secret TEXT NOT NULL,
            enabled INTEGER DEFAULT 0,
            backup_codes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (id = 1)
        )
    """)

    # Rate limit configurations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            requests_per_minute INTEGER DEFAULT 20,
            requests_per_hour INTEGER DEFAULT 200,
            requests_per_day INTEGER DEFAULT 1000,
            concurrent_requests INTEGER DEFAULT 3,
            priority INTEGER DEFAULT 0,
            is_unlimited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_key_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # Request log for rate limit tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            endpoint TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_ms INTEGER,
            status TEXT DEFAULT 'success'
        )
    """)

    # API keys
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            permissions TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP,
            expires_at TIMESTAMP
        )
    """)

    # API users (for programmatic access with isolated workspaces)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE,
            password_hash TEXT,
            api_key_hash TEXT NOT NULL,
            project_id TEXT,
            profile_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            web_login_allowed BOOLEAN DEFAULT TRUE,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # Credential policies
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credential_policies (
            id TEXT PRIMARY KEY,
            api_key_id TEXT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
            profile_id TEXT REFERENCES agent_profiles(id) ON DELETE SET NULL,
            credential_type TEXT NOT NULL,
            provider TEXT,
            inherit_admin INTEGER DEFAULT 0,
            allowed_models TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # API user credentials
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_credentials (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL REFERENCES api_users(id) ON DELETE CASCADE,
            credential_type TEXT NOT NULL,
            provider TEXT,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(api_user_id, credential_type, provider)
        )
    """)

    # API user profiles junction
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_profiles (
            api_user_id TEXT NOT NULL REFERENCES api_users(id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (api_user_id, profile_id)
        )
    """)

    # Agent runs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'running',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT
        )
    """)

    # Agent tasks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            input_data TEXT,
            output_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    """)

    # Agent logs
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Credentials
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            provider TEXT,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Subagents (global subagents independent of profiles)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subagents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tools JSON,
            model TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Permission rules for tool access control
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permission_rules (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            tool_name TEXT NOT NULL,
            tool_pattern TEXT,
            decision TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Tags for organizing sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6366f1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Junction table for session-tag relationships
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_tags (
            session_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, tag_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)

    # Session templates for starter prompts
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            prompt TEXT NOT NULL,
            profile_id TEXT,
            icon TEXT,
            category TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # Webhooks for external integrations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret TEXT,
            events JSON NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0
        )
    """)

    # Audit log for security events
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            user_type TEXT DEFAULT 'admin',
            event_type TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            details JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Knowledge base documents
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text/plain',
            file_size INTEGER DEFAULT 0,
            chunk_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # Knowledge chunks for search
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
        )
    """)

    # Session messages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input JSON,
            metadata JSON,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # Sync log for tracking changes
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            data JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # Usage tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            profile_id TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Login attempts tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            username TEXT,
            success BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Account lockout tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_lockouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            username TEXT,
            locked_until TIMESTAMP NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # API key web sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_key_sessions (
            token TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # Checkpoints for rewind functionality
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            sdk_session_id TEXT NOT NULL,
            message_uuid TEXT NOT NULL,
            message_preview TEXT,
            message_index INTEGER DEFAULT 0,
            git_ref TEXT,
            git_available BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # User preferences
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value JSON,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_type, user_id, key)
        )
    """)

    # Insert schema version
    cursor.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (25)")


@pytest.fixture(scope="function")
def test_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Create an in-memory SQLite database for each test.

    This fixture provides complete test isolation - each test gets
    a fresh database with the schema initialized.
    """
    # Create in-memory database
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create schema directly (don't use app's init_database)
    cursor = conn.cursor()
    create_test_schema(cursor)
    conn.commit()

    yield conn

    conn.close()


@pytest.fixture(scope="function")
def mock_db(test_db: sqlite3.Connection):
    """
    Patch the database module to use the test database.
    """
    @contextmanager
    def mock_get_db():
        """Mock context manager for get_db()"""
        try:
            yield test_db
            test_db.commit()
        except Exception:
            test_db.rollback()
            raise

    def mock_get_connection():
        """Mock for get_connection()"""
        return test_db

    with patch("app.db.database.get_connection", mock_get_connection):
        with patch("app.db.database.get_db", mock_get_db):
            yield test_db


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

    # Create sessions directory
    sessions_dir = workspace / "sessions"
    sessions_dir.mkdir()

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

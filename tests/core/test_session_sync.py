"""
Unit tests for session sync service.

Tests cover:
- Discovering unimported sessions from Claude Code via SDK list_sessions()
- Importing individual sessions from SDKSessionInfo
- Batch syncing sessions for a project
- Error handling and edge cases
- Integration with database operations
"""

import pytest
import time
import uuid
from unittest.mock import patch

from app.core.cli_sessions import SessionInfo as SDKSessionInfo


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.core.session_sync.database") as mock_db:
        # Default behaviors
        mock_db.get_session_by_sdk_id.return_value = None
        mock_db.get_profile.return_value = {"id": "default", "name": "Default"}
        mock_db.get_all_profiles.return_value = [{"id": "default", "name": "Default"}]
        mock_db.get_session.return_value = {
            "id": "ses-test123",
            "title": "Test Session",
            "profile_id": "default",
            "sdk_session_id": "test-sdk-id"
        }
        yield mock_db


def make_sdk_session_info(
    session_id: str = None,
    summary: str = "Test session",
    last_modified: int = None,
    file_size: int = 1024,
    custom_title: str = None,
    first_prompt: str = None,
    git_branch: str = None,
    cwd: str = None,
) -> SDKSessionInfo:
    """Helper to create SDKSessionInfo objects for testing."""
    return SDKSessionInfo(
        session_id=session_id or str(uuid.uuid4()),
        summary=summary,
        last_modified=last_modified or int(time.time() * 1000),
        file_size=file_size,
        custom_title=custom_title,
        first_prompt=first_prompt,
        git_branch=git_branch,
        cwd=cwd,
    )


# =============================================================================
# Test Module Imports
# =============================================================================

class TestSessionSyncModuleImports:
    """Verify session sync module can be imported correctly."""

    def test_session_sync_module_imports(self):
        """Session sync module should import without errors."""
        from app.core import session_sync
        assert session_sync is not None

    def test_session_sync_service_exists(self):
        """SessionSyncService class should exist."""
        from app.core.session_sync import SessionSyncService
        assert SessionSyncService is not None

    def test_global_service_instance_exists(self):
        """Global session_sync_service instance should exist."""
        from app.core.session_sync import session_sync_service
        assert session_sync_service is not None


# =============================================================================
# Test Discover Unimported Sessions
# =============================================================================

class TestDiscoverUnimportedSessions:
    """Test discovering sessions that exist on disk but not in database."""

    def test_discover_empty_directory(self, mock_database):
        """Should return empty list when SDK returns no sessions."""
        with patch("app.core.session_sync.list_sessions", return_value=[]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert result == []

    def test_discover_finds_unimported_sessions(self, mock_database):
        """Should find sessions that are not in the database."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert len(result) == 2
            session_ids = [s.session_id for s in result]
            assert session1.session_id in session_ids
            assert session2.session_id in session_ids

    def test_discover_skips_already_imported(self, mock_database):
        """Should skip sessions that are already in the database."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        # Mark session1 as already imported
        mock_database.get_session_by_sdk_id.side_effect = lambda sdk_id: (
            {"id": "ses-existing", "sdk_session_id": sdk_id}
            if sdk_id == session1.session_id else None
        )

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert len(result) == 1
            assert result[0].session_id == session2.session_id

    def test_discover_handles_sdk_error(self, mock_database):
        """Should return empty list when SDK list_sessions fails."""
        with patch("app.core.session_sync.list_sessions", side_effect=Exception("SDK error")):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert result == []

    def test_discover_sorted_by_last_modified(self, mock_database):
        """SDK list_sessions returns sessions sorted by last_modified desc."""
        # SDK itself sorts by last_modified — verify we pass them through correctly
        newer = make_sdk_session_info(last_modified=2000)
        older = make_sdk_session_info(last_modified=1000)

        with patch("app.core.session_sync.list_sessions", return_value=[newer, older]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.discover_unimported_sessions("test-project", "/tmp/test")

            assert len(result) == 2
            # Order from SDK should be preserved
            assert result[0].session_id == newer.session_id
            assert result[1].session_id == older.session_id


# =============================================================================
# Test Import Session
# =============================================================================

class TestImportSession:
    """Test importing individual sessions."""

    def test_import_session_success(self, mock_database):
        """Should successfully import a session."""
        session_info = make_sdk_session_info(summary="Hello world session")

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test",
                profile_id="default"
            )

            assert result is not None
            mock_database.create_session_with_stats.assert_called_once()

    def test_import_session_already_exists(self, mock_database):
        """Should return existing session if already imported."""
        session_info = make_sdk_session_info()
        existing_session = {"id": "ses-existing", "sdk_session_id": session_info.session_id}
        mock_database.get_session_by_sdk_id.return_value = existing_session

        from app.core.session_sync import SessionSyncService
        service = SessionSyncService()

        result = service.import_session(
            session_info=session_info,
            project_id="test-project",
            working_dir="/tmp/test"
        )

        assert result == existing_session
        mock_database.create_session_with_stats.assert_not_called()

    def test_import_session_uses_custom_title(self, mock_database):
        """Should prefer custom_title over summary."""
        session_info = make_sdk_session_info(
            custom_title="My Custom Title",
            summary="Auto-generated summary",
        )

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test"
            )

            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["title"] == "My Custom Title"

    def test_import_session_uses_summary_without_custom_title(self, mock_database):
        """Should use summary when no custom_title is set."""
        session_info = make_sdk_session_info(
            custom_title=None,
            summary="Hello, can you help me?",
        )

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test"
            )

            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["title"] == "Hello, can you help me?"

    def test_import_session_fallback_profile(self, mock_database):
        """Should fall back to default profile if specified profile doesn't exist."""
        session_info = make_sdk_session_info()

        # Profile doesn't exist for first call, then exists for default
        mock_database.get_profile.side_effect = [
            None,  # "nonexistent" doesn't exist
            {"id": "default", "name": "Default"}  # "default" exists
        ]

        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test",
                profile_id="nonexistent"
            )

            # Should use default profile
            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["profile_id"] == "default"

    def test_import_session_no_profiles_available(self, mock_database):
        """Should return None if no profiles are available."""
        session_info = make_sdk_session_info()

        mock_database.get_profile.return_value = None
        mock_database.get_all_profiles.return_value = []

        from app.core.session_sync import SessionSyncService
        service = SessionSyncService()

        result = service.import_session(
            session_info=session_info,
            project_id="test-project",
            working_dir="/tmp/test"
        )

        assert result is None
        mock_database.create_session_with_stats.assert_not_called()

    def test_import_session_includes_token_usage(self, mock_database):
        """Should include token usage from JSONL when available."""
        session_info = make_sdk_session_info()

        usage_data = {"total_tokens_in": 500, "total_tokens_out": 200}
        with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value=usage_data):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            service.import_session(
                session_info=session_info,
                project_id="test-project",
                working_dir="/tmp/test"
            )

            call_kwargs = mock_database.create_session_with_stats.call_args[1]
            assert call_kwargs["total_tokens_in"] == 500
            assert call_kwargs["total_tokens_out"] == 200


# =============================================================================
# Test Sync Project Sessions
# =============================================================================

class TestSyncProjectSessions:
    """Test batch syncing sessions for a project."""

    def test_sync_project_not_found(self, mock_database):
        """Should return error when project doesn't exist."""
        from app.core.session_sync import SessionSyncService
        service = SessionSyncService()

        mock_database.get_project.return_value = None

        result = service.sync_project_sessions("nonexistent-project")

        assert result["imported_count"] == 0
        assert "not found" in result["errors"][0].lower()

    def test_sync_imports_all_unimported(self, mock_database):
        """Should import all unimported sessions for a project."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            assert result["imported_count"] == 2
            assert result["errors"] == []
            assert len(result["sessions"]) == 2

    def test_sync_returns_imported_sessions(self, mock_database):
        """Should return list of imported session objects."""
        session = make_sdk_session_info()

        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        with patch("app.core.session_sync.list_sessions", return_value=[session]), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            assert len(result["sessions"]) == 1
            # The returned session should be from get_session
            assert result["sessions"][0]["id"] == "ses-test123"

    def test_sync_continues_when_import_fails(self, mock_database):
        """Should continue importing other sessions when one fails."""
        session1 = make_sdk_session_info()
        session2 = make_sdk_session_info()

        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        # Make first session import fail by making create_session_with_stats throw
        call_count = [0]

        def create_session_with_stats_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Database error")
            return {"id": "ses-new"}

        mock_database.create_session_with_stats.side_effect = create_session_with_stats_side_effect

        with patch("app.core.session_sync.list_sessions", return_value=[session1, session2]), \
             patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            # One succeeded (the error is caught inside import_session which returns None)
            assert result["imported_count"] == 1
            assert result["skipped_count"] == 1
            assert len(result["sessions"]) == 1

    def test_sync_no_sessions_to_import(self, mock_database):
        """Should handle case where all sessions are already imported."""
        mock_database.get_project.return_value = {
            "id": "test-project",
            "path": "test-project"
        }

        with patch("app.core.session_sync.list_sessions", return_value=[]):
            from app.core.session_sync import SessionSyncService
            service = SessionSyncService()

            result = service.sync_project_sessions("test-project")

            assert result["imported_count"] == 0
            assert result["errors"] == []
            assert result["sessions"] == []


# =============================================================================
# Test API Endpoint Integration
# =============================================================================

class TestSessionSyncAPIIntegration:
    """Test session sync API endpoint."""

    def test_sync_sessions_endpoint_exists(self):
        """The sync-sessions endpoint should exist in projects router."""
        from app.api.projects import router
        # Routes include the router prefix, so we check for the full path
        routes = [r.path for r in router.routes]
        # Check that any route ends with sync-sessions
        sync_session_routes = [r for r in routes if r.endswith("/sync-sessions")]
        assert len(sync_session_routes) > 0, f"sync-sessions endpoint not found in routes: {routes}"

    def test_list_sessions_has_sync_parameter(self):
        """The list sessions endpoint should have sync_claude_sessions parameter."""
        from app.api.sessions import list_sessions
        import inspect
        sig = inspect.signature(list_sessions)
        assert "sync_claude_sessions" in sig.parameters


# =============================================================================
# Producing-file regressions: discovery -> import -> lazy transcript backfill
# =============================================================================

@pytest.fixture
def cli_transcript(tmp_path, monkeypatch):
    from app.core import cli_sessions
    from app.core.config import settings

    projects_dir = tmp_path / "claude-projects"
    monkeypatch.setattr(settings, "claude_projects_dir", projects_dir)
    sid = str(uuid.uuid4())
    project_dir = projects_dir / cli_sessions.project_key_for_directory(str(tmp_path))
    project_dir.mkdir(parents=True)
    path = project_dir / f"{sid}.jsonl"
    records = [
        {"type": "user", "uuid": "cli-user", "parentUuid": None,
         "cwd": str(tmp_path), "timestamp": "2024-01-01T00:00:00Z",
         "message": {"role": "user", "content": "Historical question"}},
        {"type": "assistant", "uuid": "cli-assistant", "parentUuid": "cli-user",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Historical answer"}]}},
    ]
    return sid, path, records


@pytest.mark.parametrize("timestamp", [
    "2024-01-01T00:00:00Z",
    "2026-09-23T12:34:56.789Z",
    "2020-02-03T05:06:07+02:00",
])
def test_import_preserves_discovered_creation_date(mock_db, cli_transcript, timestamp):
    import json
    from datetime import datetime, timezone
    from app.core.session_sync import SessionSyncService
    from app.db import database

    sid, path, records = cli_transcript
    records[0]["timestamp"] = timestamp
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    working_dir = records[0]["cwd"]
    service = SessionSyncService()
    discovered = service.discover_unimported_sessions(None, working_dir)
    assert [info.session_id for info in discovered] == [sid]
    expected = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    assert discovered[0].created_at == int(expected.timestamp() * 1000)

    imported = service.import_session(discovered[0], None, working_dir)
    assert imported is not None
    persisted = database.get_session(imported["id"])
    assert datetime.fromisoformat(persisted["created_at"]) == expected


@pytest.mark.parametrize("created_at, expected", [
    (1704067200, "2024-01-01T00:00:00+00:00"),
    (1704067200000, "2024-01-01T00:00:00+00:00"),
    (None, None), (0, None), ("invalid", None),
    (float("nan"), None), (float("inf"), None), (10 ** 100, None),
])
def test_import_creation_date_compatibility(mock_database, created_at, expected):
    from app.core.session_sync import SessionSyncService

    info = make_sdk_session_info()
    info.created_at = created_at
    with patch("app.core.session_sync.get_session_cost_from_jsonl", return_value={}):
        assert SessionSyncService().import_session(info, None, "/workspace") is not None
    assert mock_database.create_session_with_stats.call_args.kwargs["created_at"] == expected


@pytest.mark.parametrize("timestamp", [None, "not-a-date", 123])
def test_import_missing_or_malformed_file_date_falls_back(mock_db, cli_transcript, timestamp):
    import json
    from datetime import datetime, timezone
    from app.core.session_sync import SessionSyncService

    sid, path, records = cli_transcript
    records[0]["timestamp"] = timestamp
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    service = SessionSyncService()
    discovered = service.discover_unimported_sessions(None, records[0]["cwd"])
    assert [info.session_id for info in discovered] == [sid]
    assert discovered[0].created_at is None
    before = datetime.now(timezone.utc)
    imported = service.import_session(discovered[0], None, records[0]["cwd"])
    assert imported is not None
    assert before <= datetime.fromisoformat(imported["created_at"]) <= datetime.now(timezone.utc)


@pytest.mark.parametrize("damaged", [
    b'\xffmalformed record',
    b'{"type":"user","uuid":"damaged","parentUuid":"cli-user",'
    b'"message":{"role":"user","content":"corrupted \xff text"}}',
])
async def test_imported_archive_backfills_valid_records_around_bad_utf8(
    mock_db, cli_transcript, damaged, caplog, monkeypatch,
):
    import json
    import os
    from app.core.history_backfill import ensure_backfilled
    from app.core.session_sync import SessionSyncService
    from app.db import session_events

    # SQLite cannot implement PostgreSQL advisory locks. This single-reader
    # regression exercises real inserts/projection, not lock concurrency.
    mock_db.create_function("hashtext", 1, lambda value: 0)
    mock_db.create_function("pg_advisory_xact_lock", 1, lambda value: None)
    # Match psycopg's JSONB decoding for the store columns, which the shim
    # otherwise returns as TEXT. All values still come from the real writer.
    import sqlite3

    def store_json_row(cursor, values):
        row = sqlite3.Row(cursor, values)
        if "entry" in row.keys() or "data" in row.keys():
            return {key: json.loads(row[key]) if key in {"entry", "data"}
                    and isinstance(row[key], str) else row[key] for key in row.keys()}
        return row

    mock_db.row_factory = store_json_row
    from tests import pg_shim
    translate_sql = pg_shim.translate_sql
    monkeypatch.setattr(pg_shim, "translate_sql", lambda sql: translate_sql(sql).replace(
        "EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)", "strftime('%s', 'now')",
    ))
    sid, path, records = cli_transcript
    path.write_bytes(json.dumps(records[0]).encode("utf-8") + b"\n" + damaged + b"\n"
                     + json.dumps(records[1]).encode("utf-8") + b"\n")
    old = time.time() - 3600  # archived: outside the live-history freshness guard
    os.utime(path, (old, old))
    service = SessionSyncService()
    discovered = service.discover_unimported_sessions(None, records[0]["cwd"])
    assert [info.session_id for info in discovered] == [sid]
    imported = service.import_session(discovered[0], None, records[0]["cwd"])
    assert imported is not None
    assert mock_db.execute("SELECT COUNT(*) FROM session_store_entries").fetchone()[0] == 0
    assert session_events.get_events(imported["id"]) == []

    await ensure_backfilled(imported["id"])

    rows = session_events.get_events(imported["id"])
    assert [row["event_type"] for row in rows] == ["message.user", "message.assistant"]
    assert [row["payload"]["content"] for row in rows] == [
        "Historical question", "Historical answer",
    ]
    assert all(row["turn_id"] == "backfill" for row in rows)
    assert mock_db.execute("SELECT COUNT(*) FROM session_store_entries").fetchone()[0] == 2
    assert f"Skipping undecodable line 2 in {path}" in caplog.text

    await ensure_backfilled(imported["id"])
    assert len(session_events.get_events(imported["id"])) == 2

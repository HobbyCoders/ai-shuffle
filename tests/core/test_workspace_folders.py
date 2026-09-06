from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from app.core import query_options as qo
from app.core.session_pool import SessionState, requires_reconnect
from app.core.workspace_folders import normalize_workspace_project_ids, workspace_directories
from app.db import database as db


def test_folders_persist_per_session_and_can_be_cleared():
    db.create_profile(profile_id="folders", name="Folders", description=None, config={})
    for sid in ("chat-a", "chat-b"):
        db.create_session(session_id=sid, profile_id="folders")
    db.set_session_run_binding("chat-a", workspace_project_ids=["docs", "api", "docs"])
    assert normalize_workspace_project_ids(db.get_session("chat-a")["workspace_project_ids"]) == ["docs", "api"]
    assert normalize_workspace_project_ids(db.get_session("chat-b")["workspace_project_ids"]) == []
    db.set_session_run_binding("chat-a", workspace_project_ids=[])
    assert normalize_workspace_project_ids(db.get_session("chat-a")["workspace_project_ids"]) == []


@pytest.mark.parametrize("value", [{}, [None], [""], ["x"] * 51])
def test_rejects_invalid_folder_lists(value):
    with pytest.raises(ValueError):
        normalize_workspace_project_ids(value)


def test_resolves_projects_on_current_runner_and_excludes_main():
    with patch.object(db, "get_project", return_value={"name": "Docs"}), patch(
        "app.core.project_resolver.resolve_existing_cwd", return_value="/runner/docs"
    ) as resolve:
        assert workspace_directories(["main", "docs", "docs"], "main") == ["/runner/docs"]
        resolve.assert_called_once_with({"project_id": "docs"})


def test_missing_checkout_reports_project():
    with patch.object(db, "get_project", return_value={"name": "Docs"}), patch(
        "app.core.project_resolver.resolve_existing_cwd", return_value=None
    ), pytest.raises(ValueError, match="Docs.*no checkout"):
        workspace_directories(["docs"])


def test_scoped_api_user_cannot_add_another_project():
    with patch.object(db, "get_api_user", return_value={"project_id": "main"}), pytest.raises(ValueError, match="Access denied"):
        workspace_directories(["docs"], "main", "restricted-user")


def test_query_merges_workspace_and_profile_directories():
    with patch.object(qo, "get_security_instructions", return_value=""), patch(
        "app.core.workspace_folders.workspace_directories", return_value=["/work/docs"]
    ) as resolve:
        opts, _ = qo.build_options_from_profile(
            profile={"id": "t", "config": {"model": "opus", "add_dirs": ["/work/shared"]}},
            overrides={"workspace_project_ids": ["docs"]},
        )
        assert "/work/docs" in opts.add_dirs
        assert "/work/shared" in opts.add_dirs
        resolve.assert_called_once_with(["docs"], None, None)


def test_saved_folders_restore_and_explicit_empty_override_clears_them():
    profile = db.create_profile(profile_id="restore-folders", name="Folders", description=None, config={"model": "opus"})
    db.create_session(session_id="restore-folders", profile_id=profile["id"])
    db.set_session_run_binding("restore-folders", workspace_project_ids=["docs"])
    with patch.object(qo, "get_security_instructions", return_value=""), patch(
        "app.core.workspace_folders.workspace_directories",
        side_effect=lambda value, *_: ["/work/docs"] if normalize_workspace_project_ids(value) else [],
    ):
        restored, _ = qo.build_options_from_profile(profile=profile, session_id="restore-folders")
        cleared, _ = qo.build_options_from_profile(profile=profile, session_id="restore-folders", overrides={"workspace_project_ids": []})
        assert "/work/docs" in restored.add_dirs
        assert "/work/docs" not in (cleared.add_dirs or [])


def test_existing_database_gains_nullable_folders_column(mock_db):
    mock_db.execute("ALTER TABLE sessions DROP COLUMN workspace_project_ids")
    mock_db.execute("DELETE FROM schema_version")
    mock_db.execute("INSERT INTO schema_version (version) VALUES (85)")
    mock_db.commit()
    db.init_database()
    assert "workspace_project_ids" in {row[1] for row in mock_db.execute("PRAGMA table_info(sessions)")}


@pytest.mark.parametrize("before,after,expected", [(["/docs"], [], True), ([], ["/docs"], True), (["/docs"], ["/docs"], False)])
def test_folder_changes_reconnect_provider(before, after, expected):
    state = SessionState(adapter=MagicMock(), connect_options=ClaudeAgentOptions(add_dirs=before))
    assert requires_reconnect(state, ClaudeAgentOptions(add_dirs=after)) is expected

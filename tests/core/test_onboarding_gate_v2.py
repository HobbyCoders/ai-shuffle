"""Onboarding-gate contract under v2-projects (Phase 3 P5).

Pins what ``AuthService.is_onboarding_complete`` requires: an admin, the
``onboarding_complete`` flag, and — in local mode only — a per-instance
workspace-root pick. Under v2-projects the workspace root is where project
*checkouts* materialize; projects are origin-keyed (``projects_v2``) and need
NO per-install/per-project registration. This test locks in that there is no
project-level gate at all.
"""

from unittest.mock import patch

from app.core.auth import auth_service
from app.core.config import settings
from app.db import database


def _seed_admin_and_onboarding():
    database.create_admin("admin", "hash")
    database.set_system_setting("onboarding_complete", "true")
    database.set_system_setting("onboarding_started", "true")


def _workspace_key():
    return f"workspace_path:{settings.effective_instance_id}"


def test_local_mode_complete_when_admin_and_workspace_present(mock_db):
    """admin + onboarding_complete + per-instance workspace row → complete."""
    _seed_admin_and_onboarding()
    database.set_system_setting(_workspace_key(), "/some/workspace/root")

    with patch("app.core.platform.is_local_mode", return_value=True):
        assert auth_service.is_onboarding_complete() is True


def test_local_mode_incomplete_when_no_workspace_row(mock_db):
    """No per-instance workspace row (and no legacy global) → incomplete.

    This is the ONLY per-install requirement — the workspace-root pick.
    """
    _seed_admin_and_onboarding()

    with patch("app.core.platform.is_local_mode", return_value=True):
        assert auth_service.is_onboarding_complete() is False


def test_non_local_mode_complete_without_workspace_row(mock_db):
    """Docker/non-local uses a fixed default root → no per-instance pick needed."""
    _seed_admin_and_onboarding()

    with patch("app.core.platform.is_local_mode", return_value=False):
        assert auth_service.is_onboarding_complete() is True


def test_project_registration_not_required(mock_db):
    """v2 semantics: with the workspace root chosen, the gate is complete with
    ZERO projects_v2/checkouts/legacy-projects rows.

    The gate references no project table — there is no per-project onboarding to
    satisfy. This is the whole point of the v2 reframe: workspace-root pick only.
    """
    _seed_admin_and_onboarding()
    database.set_system_setting(_workspace_key(), "/some/workspace/root")

    # No projects_v2 / checkouts / legacy projects rows exist at all.
    with patch("app.core.platform.is_local_mode", return_value=True):
        assert auth_service.is_onboarding_complete() is True

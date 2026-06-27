"""
Regression tests for the cloud-offload HTTP status mapping
(``_create_offload_via_registry``).

The frontend's "Stash & switch" retry keys solely on a 409 from this path
(``message-sender.ts``). So the primary must hand back a 409 ONLY for the
worker's genuine dirty-tree conflict, and a DIFFERENT status when the target
device is simply unreachable — otherwise an offline device shows a misleading
"Stash & switch" button that can never succeed.

Everything is mocked at the relay / database boundaries — no live peer, no DB.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.sessions_cloud import (
    _create_offload_via_registry,
    CreateCloudSessionRequest,
)
from app.core import cloud_relay


DIRTY_MSG = "Cannot checkout: you have uncommitted changes. Commit or stash them first."


async def _passthru(fn, *args, **kwargs):
    """Stand-in for ``run_sync`` that just calls the (mocked) sync fn inline."""
    return fn(*args, **kwargs)


def _body(**over):
    base = dict(project_id="proj1", prompt="hi", target_instance_id="dev-2")
    base.update(over)
    return CreateCloudSessionRequest(**base)


@pytest.fixture
def patched():
    """Patch every boundary the offload helper touches; yield the key mocks."""
    with patch("app.api.sessions_cloud.database") as db, \
         patch("app.api.sessions_cloud.get_api_user_from_request", return_value=None), \
         patch("app.api.sessions_cloud.run_sync", side_effect=_passthru), \
         patch(
             "app.api.sessions_cloud._resolve_github_repo",
             return_value=("https://github.com/o/r.git", "o/r", "main"),
         ), \
         patch(
             "app.core.cloud_relay.resolve_install_base_url",
             return_value="http://dev-2:1455",
         ) as resolve, \
         patch("app.core.cloud_relay.cluster_intake", new_callable=AsyncMock) as intake:
        db.get_project.return_value = {"id": "proj1", "name": "r"}
        yield MagicMock(db=db, resolve=resolve, intake=intake)


# ---------------------------------------------------------------------------
# Unreachable device -> 503 (NOT 409): never offer "Stash & switch"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offline_at_resolve_is_503(patched):
    patched.resolve.side_effect = cloud_relay.InstallOffline("Device 'dev-2' is offline")
    with pytest.raises(HTTPException) as exc:
        await _create_offload_via_registry(MagicMock(), _body(), "hi")
    assert exc.value.status_code == 503
    assert "offline" in exc.value.detail
    patched.intake.assert_not_called()


@pytest.mark.asyncio
async def test_offline_during_intake_is_503(patched):
    patched.intake.side_effect = cloud_relay.InstallOffline("Device 'dev-2' is offline")
    with pytest.raises(HTTPException) as exc:
        await _create_offload_via_registry(MagicMock(), _body(), "hi")
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# Genuine dirty-tree conflict from the worker -> 409 preserved with its detail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_dirty_tree_409_is_preserved(patched):
    patched.intake.side_effect = cloud_relay.CloudRelayError(
        "peer returned 409", status_code=409, detail=DIRTY_MSG
    )
    with pytest.raises(HTTPException) as exc:
        await _create_offload_via_registry(MagicMock(), _body(), "hi")
    assert exc.value.status_code == 409
    # Detail must survive so the frontend can show it next to "Stash & switch".
    assert exc.value.detail == DIRTY_MSG


@pytest.mark.asyncio
async def test_other_relay_error_collapses_to_502(patched):
    patched.intake.side_effect = cloud_relay.CloudRelayError(
        "boom", status_code=500, detail="internal"
    )
    with pytest.raises(HTTPException) as exc:
        await _create_offload_via_registry(MagicMock(), _body(), "hi")
    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_success_returns_remote_session(patched):
    patched.intake.return_value = {"session_id": "ses-abc123", "query_started": True}
    resp = await _create_offload_via_registry(MagicMock(), _body(), "hi")
    assert resp.session_id == "ses-abc123"
    assert resp.remote_session_id == "ses-abc123"
    assert resp.query_started is True

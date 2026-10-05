"""Tests for the push-direction cross-device move
(POST /api/v1/sessions/{id}/move in app/api/sessions.py).

Move is adopt's mirror image: instead of "resume THIS session HERE" it says
"run this session on THAT device from now on", so it can be driven from any
install. The destination performs the canonical authenticated Git/context
handoff; the API never writes a database-only pin. Preflight gates:
  - target already owns it → 200 no-op (``moved=False``);
  - unknown install → 404; offline/incapable install → 409
    (``capable_runners(None, {})`` — online + capability only);
  - project session whose target has no ACTIVE BASE checkout → 409
    (``network.git.eligible``; a branch-only worktree is not a base);
  - actively streaming → 409 (same gate as /adopt), so turn-end persist
    cannot restore the old disk's ``sdk_session_id``. ``pending_live_run``
    stays ``false``. The destination retains its imported native context id.

Fixtures mirror tests/api/test_adopt_preflight_v2.py. Checkout-gate tests seed
real ``checkouts`` rows (mirror tests/api/test_worktree_remote_target.py) and
do not patch ``capable_runners`` for the host predicate.
"""

from unittest.mock import AsyncMock, patch

from app.network import control

import pytest
from fastapi.testclient import TestClient

from app.db import checkouts as checkouts_db
from app.db import projects_v2 as projects_v2_db
from tests.api.helpers import app, mock_api_user, not_streaming  # noqa: F401  (fixtures)


@pytest.fixture
def mock_database():
    with patch("app.api.sessions.database") as mock_db, \
            patch("app.api.sessions_helpers.database", mock_db):
        mock_db.get_session_tags.return_value = []
        mock_db.session_has_forks.return_value = False
        mock_db.get_installs_map.return_value = {}
        mock_db.get_project.return_value = None
        yield mock_db


@pytest.fixture(autouse=True)
def mock_handoff():
    with patch("app.network.handoff.move", new=AsyncMock(return_value=True)) as move:
        yield move


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


TARGET = "target-install-abc"


def _seed_pv2():
    """A real projects_v2 row so checkout inserts satisfy the FK."""
    return projects_v2_db.upsert_project("git:move-test-repo", name="Move Test")


def _give_target_a_checkout(tmp_path, project_v2_id, *, branch=None, runner=TARGET):
    """Seed a checkout row on the move target (real DB, not a mock)."""
    checkouts_db.upsert_checkout(
        project_v2_id,
        runner,
        str(tmp_path / "repo") if branch is None else str(tmp_path / branch),
        branch=branch,
        status="active",
    )


def _session(**overrides):
    row = {
        "id": "test-session-id",
        "title": "T",
        "profile_id": "default",
        "instance_id": "other-install-xyz",
        "project_id": None,
        "project_v2_id": None,
        "worktree_id": None,
        "worktree_branch": None,
        "status": "active",
        "is_active": False,
        "sdk_session_id": "sdk-1",
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }
    row.update(overrides)
    return row


def _move(client, target=TARGET):
    return client.post(
        "/api/v1/sessions/test-session-id/move",
        json={"target_instance_id": target},
    )


def test_same_owner_is_a_noop(client, mock_database):
    """Target already owns the session → 200, moved=False, no write."""
    mock_database.get_session.return_value = _session(instance_id=TARGET)

    response = _move(client)

    assert response.status_code == 200
    data = response.json()
    assert data["moved"] is False
    assert data["status"] == "already_there"
    assert data["instance_id"] == TARGET
    mock_database.adopt_session.assert_not_called()


def test_unknown_target_404s(client, mock_database):
    """No registry row for the target install → 404, no write."""
    mock_database.get_session.return_value = _session()
    mock_database.get_install.return_value = None

    response = _move(client)

    assert response.status_code == 404
    assert "Unknown device" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_offline_target_409s(client, mock_database):
    """Target is registered but not an online+capable runner → 409, no write."""
    mock_database.get_session.return_value = _session()
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}

    with patch("app.core.placement.capable_runners", return_value=[]):
        response = _move(client)

    assert response.status_code == 409
    assert "Studio Mac is offline" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_project_session_without_checkout_on_target_409s(client, mock_database):
    """Project session + target has no checkout row at all → 409, no write.

    The online check (``capable_runners(None, {})``) passes; the host
    predicate is the real ``has_active_base_checkout``, not a mock of
    ``capable_runners(project_v2_id)``.
    """
    pv2 = _seed_pv2()
    mock_database.get_session.return_value = _session(
        project_id="proj-1", project_v2_id=pv2["id"]
    )
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}

    with patch("app.core.placement.capable_runners", return_value=[TARGET]):
        response = _move(client)

    assert response.status_code == 409
    assert "isn't checked out on Studio Mac" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_branch_only_checkout_on_target_409s(client, mock_database, tmp_path):
    """``capable_runners`` would accept this row; the base-checkout gate must
    not — Phase 3 never clones, so there is nothing to fork from on the target.
    """
    pv2 = _seed_pv2()
    mock_database.get_session.return_value = _session(
        project_id="proj-1", project_v2_id=pv2["id"]
    )
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    _give_target_a_checkout(tmp_path, pv2["id"], branch="some/other-branch")

    with patch("app.core.placement.capable_runners", return_value=[TARGET]):
        response = _move(client)

    assert response.status_code == 409
    assert "isn't checked out on Studio Mac" in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()


def test_active_base_checkout_on_target_moves(client, mock_database, mock_handoff, tmp_path):
    """Idle project session + active BASE checkout on the target → 200."""
    pv2 = _seed_pv2()
    session = _session(project_id="proj-1", project_v2_id=pv2["id"])
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    _give_target_a_checkout(tmp_path, pv2["id"])

    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.core.turn_claim.notify_turns_changed") as notify:
        response = _move(client)

    assert response.status_code == 200
    data = response.json()
    assert data["moved"] is True
    assert data["status"] == "moved"
    assert data["instance_id"] == TARGET
    mock_handoff.assert_awaited_once_with(session, TARGET)
    mock_database.adopt_session.assert_not_called()
    notify.assert_called_once_with("test-session-id")


def test_happy_path_moves_and_repins(client, mock_database, mock_handoff):
    """Idle projectless session → confirmed destination handoff, no local pin."""
    session = _session()
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}

    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.core.turn_claim.notify_turns_changed") as notify:
        response = _move(client)

    assert response.status_code == 200
    data = response.json()
    assert data["moved"] is True
    assert data["status"] == "moved"
    assert data["instance_id"] == TARGET
    assert data["pending_live_run"] is False
    mock_handoff.assert_awaited_once_with(session, TARGET)
    mock_database.adopt_session.assert_not_called()
    notify.assert_called_once_with("test-session-id")

    mock_database.update_session.assert_not_called()


def test_live_run_409s_and_does_not_move(client, mock_database):
    """A streaming session is refused so turn-end persist cannot restore the
    old disk's ``sdk_session_id`` after a successful move. The 409 names the
    CURRENT owner, not the move target."""
    session = _session()  # instance_id = other-install-xyz
    mock_database.get_session.return_value = session

    def _install(iid):
        if iid == TARGET:
            return {"device_name": "Studio Mac"}
        if iid == "other-install-xyz":
            return {"device_name": "Laptop"}
        return None

    mock_database.get_install.side_effect = _install

    with patch("app.core.streaming_status.has_active_run", return_value=True), \
            patch("app.core.placement.capable_runners", return_value=[TARGET]):
        response = _move(client)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Laptop" in detail
    assert "Studio Mac" not in detail
    mock_database.adopt_session.assert_not_called()
    mock_database.update_session.assert_not_called()


def test_move_does_not_clear_imported_native_history(client, mock_database, mock_handoff):
    """Only the receiver decides the imported SDK identity; the sender cannot stat it."""
    session = _session(sdk_session_id="cli-uuid-1")
    mock_database.get_session.return_value = session
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}

    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.api.sessions.apply_sdk_session_keep") as keep, \
            patch("app.core.turn_claim.notify_turns_changed"):
        response = _move(client)

    assert response.status_code == 200
    mock_handoff.assert_awaited_once_with(session, TARGET)
    keep.assert_not_called()
    mock_database.adopt_session.assert_not_called()
    mock_database.update_session.assert_not_called()


def test_blank_target_is_rejected(client, mock_database):
    """An empty target_instance_id never reaches the write path."""
    mock_database.get_session.return_value = _session()

    response = _move(client, target="")

    assert response.status_code in (400, 422)
    mock_database.adopt_session.assert_not_called()


@pytest.mark.parametrize("error", [
    control.RunnerUnreachable("Destination offline"),
    control.ControlRefused("Worker removed or permission denied"),
    ValueError("Git push failed; ownership has not moved"),
])
def test_handoff_failure_never_falls_back_to_pin(client, mock_database, mock_handoff, error):
    mock_database.get_session.return_value = _session()
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    mock_handoff.side_effect = error
    with patch("app.core.placement.capable_runners", return_value=[TARGET]):
        response = _move(client)
    assert response.status_code == 409
    assert str(error) in response.json()["detail"]
    mock_database.adopt_session.assert_not_called()
    mock_database.update_session.assert_not_called()


def test_checkout_preflight_failure_is_closed(client, mock_database, mock_handoff):
    mock_database.get_session.return_value = _session(project_v2_id="project")
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.db.checkouts.has_active_base_checkout", side_effect=RuntimeError("DB unavailable")):
        response = _move(client)
    assert response.status_code == 503
    mock_handoff.assert_not_called()
    mock_database.adopt_session.assert_not_called()


def test_unresolved_legacy_project_cannot_skip_checkout_gate(client, mock_database, mock_handoff):
    mock_database.get_session.return_value = _session(project_id="legacy-project")
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.core.project_resolver.resolve_project_v2", return_value=None):
        response = _move(client)
    assert response.status_code == 409
    mock_handoff.assert_not_called()
    mock_database.adopt_session.assert_not_called()


@pytest.mark.parametrize("api_user", [
    {"id": "different-user"},
    {"id": "owner-user", "profile_ids": ["not-default"]},
    {"id": "owner-user", "project_id": "not-project"},
])
def test_move_preserves_api_user_ownership_and_scope(client, mock_database, mock_api_user, mock_handoff, api_user):
    mock_database.get_session.return_value = _session(api_user_id="owner-user", project_id="project")
    mock_api_user.return_value = api_user
    response = _move(client)
    assert response.status_code == 403
    mock_handoff.assert_not_called()
    mock_database.adopt_session.assert_not_called()


def test_canonical_target_owner_is_a_noop(client, mock_database, mock_handoff):
    mock_database.get_session.return_value = _session(owner_device_id=TARGET)
    response = _move(client)
    assert response.status_code == 200
    assert response.json()["moved"] is False
    mock_handoff.assert_not_called()


def test_destination_already_owns_session_returns_noop(client, mock_database, mock_handoff):
    mock_database.get_session.return_value = _session()
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    mock_handoff.return_value = False
    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.core.turn_claim.notify_turns_changed"):
        response = _move(client)
    assert response.status_code == 200
    assert response.json()["moved"] is False
    assert response.json()["status"] == "already_there"
    mock_database.adopt_session.assert_not_called()


@pytest.mark.parametrize("error", [
    control.RunnerUnreachable("Destination offline"),
    control.ControlRefused("Destination revoked"),
])
def test_live_checkout_query_refusal_keeps_its_reason(client, mock_database, mock_handoff, error):
    mock_database.get_session.return_value = _session(project_v2_id="project")
    mock_database.get_install.return_value = {"device_name": "Studio Mac"}
    with patch("app.core.placement.capable_runners", return_value=[TARGET]), \
            patch("app.network.git.eligible", new=AsyncMock(side_effect=error)):
        response = _move(client)
    assert response.status_code == 409
    assert str(error) in response.json()["detail"]
    mock_handoff.assert_not_called()
    mock_database.adopt_session.assert_not_called()


# #1819: "Move when this turn ends" is queued on the owning worker.

def test_queued_move_routes_status_queue_and_cancel(client, mock_database):
    mock_database.get_session.return_value = _session()
    route = AsyncMock(side_effect=lambda session, op, target=None, stop=False: {"op": op, "target": target, "stop": stop})
    with patch("app.core.session_handoff.route", route):
        assert client.get("/api/v1/sessions/test-session-id/handoff").json()["op"] == "status"
        queued = client.post("/api/v1/sessions/test-session-id/handoff",
                             json={"target_instance_id": TARGET, "stop": True}).json()
        assert queued == {"op": "queue", "target": TARGET, "stop": True}
        assert client.delete("/api/v1/sessions/test-session-id/handoff").json()["op"] == "cancel"


def test_resume_here_queues_a_move_to_this_worker(client, mock_database):
    from types import SimpleNamespace
    mock_database.get_session.return_value = _session()
    route = AsyncMock(return_value={"queued": True, "worker": "DesktopWorker"})
    with patch("app.core.session_handoff.route", route), \
            patch("app.network.engine.context", return_value=SimpleNamespace(device_id="dev-here")):
        response = client.post("/api/v1/sessions/test-session-id/handoff", json={})
    assert response.status_code == 200
    assert route.await_args.args[2] == "dev-here"


def test_queued_move_refusal_is_a_409_with_the_reason(client, mock_database):
    mock_database.get_session.return_value = _session()
    with patch("app.core.session_handoff.route",
               AsyncMock(side_effect=ValueError("A move to DesktopWorker is already queued. Cancel it first."))):
        response = client.post("/api/v1/sessions/test-session-id/handoff", json={"target_instance_id": TARGET})
    assert response.status_code == 409
    assert response.json()["detail"] == "A move to DesktopWorker is already queued. Cancel it first."


def test_subagent_chat_refuses_owner_move_handoff_adopt_and_auto_merge(client, mock_database):
    """A subagent's chat is read-and-message only: moves commit and push, and
    auto-merge acts on its PR, so the owner-facing endpoints refuse them."""
    from app.api.git_owner import SUBAGENT_GIT_REFUSED

    mock_database.get_session.return_value = _session(is_worker=True, parent_session_id="main-chat")
    with patch("app.core.auto_merge.enable", new=AsyncMock()) as enable, \
            patch("app.core.auto_merge.disable", new=AsyncMock(return_value={"enabled": False})) as disable:
        responses = [
            _move(client),
            client.post("/api/v1/sessions/test-session-id/handoff", json={"target_instance_id": TARGET}),
            client.post("/api/v1/sessions/test-session-id/adopt", json={}),
            client.put("/api/v1/sessions/test-session-id/auto-merge", json={"enabled": True}),
        ]
        for response in responses:
            assert response.status_code == 403, response.text
            assert response.json()["detail"] == SUBAGENT_GIT_REFUSED
        enable.assert_not_called()
        # Switching it off is never refused, so nothing can be stranded on.
        off = client.put("/api/v1/sessions/test-session-id/auto-merge", json={"enabled": False})
        assert off.status_code == 200, off.text
        disable.assert_awaited_once()
    mock_database.adopt_session.assert_not_called()


def test_startsession_peer_can_still_be_moved(client, mock_database):
    """A peer started with StartSession is a top-level chat, not a subagent."""
    mock_database.get_session.return_value = _session(
        instance_id=TARGET, is_worker=False, parent_session_id="starting-chat")
    response = _move(client)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "already_there"

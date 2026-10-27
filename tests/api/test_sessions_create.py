"""POST /api/v1/sessions: an empty session from the composer's picks.

The composer's call button creates a normal conversation this way and then
starts a call on it. It mirrors the chat WebSocket's first-message create but
submits no turn, and it never creates a voice-lane session.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.sessions_create as create_api
import app.api.sessions_worktree as worktree_api
from app.api.auth import require_auth


@pytest.fixture
def db():
    """One mock behind every module the endpoint reads the database through."""
    with patch.object(create_api, "database") as mock_db, \
            patch.object(worktree_api, "database", mock_db), \
            patch("app.api.common.database", mock_db):
        mock_db.get_profile.side_effect = lambda pid: {"id": pid} if pid in ("agent-a", "agent-b") else None
        mock_db.get_all_profiles.return_value = [{"id": "agent-a"}]
        mock_db.get_project.return_value = None
        yield mock_db


@pytest.fixture
def api_user():
    with patch.object(create_api, "get_api_user_from_request") as mock:
        mock.return_value = None
        yield mock


@pytest.fixture
def client(db, api_user, monkeypatch):
    @contextmanager
    def _txn():
        yield "txn"

    monkeypatch.setattr("app.db.base.transaction", _txn)
    broadcast = MagicMock()
    monkeypatch.setattr(create_api, "_broadcast_session_created", broadcast)
    from app.api.sessions import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "token"
    with TestClient(app) as test_client:
        test_client.broadcast = broadcast
        yield test_client


def _created(db):
    db.create_session.assert_called_once()
    return db.create_session.call_args.kwargs


def test_creates_a_projectless_conversation_with_the_composer_picks(client, db):
    response = client.post("/api/v1/sessions", json={
        "profile_id": "agent-b",
        "mode": "chat",
        "overrides": {"model": "sonnet", "effort": "high", "fast": False, "context_window": 200000},
    })

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "chat"
    kwargs = _created(db)
    assert kwargs["session_id"] == body["session_id"]
    assert kwargs["profile_id"] == "agent-b"
    assert kwargs["project_id"] is None and kwargs["project_v2_id"] is None
    assert kwargs["mode"] == "chat"
    assert kwargs["is_active"] is True
    assert kwargs["title"] == ""
    assert (kwargs["model"], kwargs["effort"], kwargs["fast"], kwargs["context_window"]) == (
        "sonnet", "high", False, 200000)
    assert "worktree_id" not in kwargs
    client.broadcast.assert_called_once_with(body["session_id"])
    # No turn and no first message: the session waits for the call or a send.
    db.add_session_message.assert_not_called()


def test_project_session_goes_through_origin_access_and_keeps_permission_mode(client, db):
    access = {"legacy": {"id": "proj-1", "settings": {}}, "project_v2": {"id": "pv2-1"}, "origin": "git:x"}
    with patch.object(create_api, "require_origin_access", return_value=access) as gate:
        response = client.post("/api/v1/sessions", json={
            "project_id": "proj-1", "profile_id": "agent-a",
            "overrides": {"permission_mode": "acceptEdits", "workspace_project_ids": ["proj-2"]},
        })

    assert response.status_code == 200
    assert gate.call_args.kwargs["project_id"] == "proj-1"
    kwargs = _created(db)
    assert kwargs["project_id"] == "proj-1"
    assert kwargs["project_v2_id"] == "pv2-1"
    assert kwargs["permission_mode"] == "acceptEdits"
    assert kwargs["mode"] == "code"
    db.set_session_run_binding.assert_called_once()
    assert db.set_session_run_binding.call_args.kwargs["workspace_project_ids"] == ["proj-2"]


def test_voice_lane_is_refused_before_anything_is_created(client, db):
    response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "mode": "voice"})

    assert response.status_code == 400
    db.create_session.assert_not_called()


def test_unknown_agent_is_refused_before_anything_is_created(client, db):
    response = client.post("/api/v1/sessions", json={"profile_id": "gone"})

    assert response.status_code == 400
    assert "no longer exists" in response.json()["detail"]
    db.create_session.assert_not_called()


def test_omitted_agent_falls_back_to_the_first_agent(client, db):
    response = client.post("/api/v1/sessions", json={})

    assert response.status_code == 200
    assert _created(db)["profile_id"] == "agent-a"


def test_restricted_key_cannot_pick_another_agent(client, db, api_user):
    api_user.return_value = {"id": "key-1", "profile_ids": ["agent-a"]}

    denied = client.post("/api/v1/sessions", json={"profile_id": "agent-b"})
    assert denied.status_code == 403
    db.create_session.assert_not_called()

    allowed = client.post("/api/v1/sessions", json={})
    assert allowed.status_code == 200
    kwargs = _created(db)
    assert kwargs["profile_id"] == "agent-a"
    assert kwargs["api_user_id"] == "key-1"


def test_existing_worktree_row_is_stamped_on_the_session(client, db):
    access = {"legacy": {"id": "proj-1", "settings": {}}, "project_v2": {"id": "pv2-1"}, "origin": "git:x"}
    db.get_worktree.return_value = {"id": "wt-1", "repository_id": "repo-1", "branch": "feat/x", "path": "/w/x"}
    db.get_git_repository_by_project.return_value = {"id": "repo-1"}
    with patch.object(create_api, "require_origin_access", return_value=access):
        response = client.post("/api/v1/sessions", json={
            "project_id": "proj-1", "profile_id": "agent-a", "worktree_id": "wt-1"})

    assert response.status_code == 200
    kwargs = _created(db)
    assert (kwargs["worktree_id"], kwargs["worktree_branch"], kwargs["worktree_path"]) == ("wt-1", "feat/x", "/w/x")


def test_worktree_from_another_project_is_refused(client, db):
    access = {"legacy": {"id": "proj-1", "settings": {}}, "project_v2": {"id": "pv2-1"}, "origin": "git:x"}
    db.get_worktree.return_value = {"id": "wt-1", "repository_id": "repo-other"}
    db.get_git_repository_by_project.return_value = {"id": "repo-1"}
    with patch.object(create_api, "require_origin_access", return_value=access):
        response = client.post("/api/v1/sessions", json={
            "project_id": "proj-1", "profile_id": "agent-a", "worktree_id": "wt-1"})

    assert response.status_code == 404
    db.create_session.assert_not_called()


class TestRunsOnAnotherWorker:
    """#700: the composer's "Runs on" pick creates the conversation on that
    worker, as a typed first message does, so a call can attach to it."""

    PEER = "peer-device-1"

    @pytest.fixture
    def owner(self, db):
        from unittest.mock import AsyncMock
        from app.network import control
        db.get_install.return_value = {"id": self.PEER, "device_name": "Office PC", "online": True, "approved": True}
        db.get_session.side_effect = lambda sid: {"id": sid, "instance_id": self.PEER, "context_window": None}
        with patch.object(control, "owner_of", return_value=self.PEER) as owner_of, \
                patch.object(control, "create_session", new=AsyncMock(return_value={"session_id": "x"})) as create:
            yield {"owner_of": owner_of, "create": create}

    def test_creates_the_mirror_here_and_the_session_on_the_owner(self, client, db, owner):
        response = client.post("/api/v1/sessions", json={
            "profile_id": "agent-a", "mode": "chat", "target_instance_id": self.PEER,
            "overrides": {"model": "sonnet", "permission_mode": "default"},
        })

        assert response.status_code == 200, response.text
        session_id = response.json()["session_id"]
        kwargs = _created(db)
        assert kwargs["target_instance_id"] == self.PEER
        owner["create"].assert_awaited_once()
        device, session, turn = owner["create"].await_args.args
        assert device == self.PEER and turn is None
        assert session["id"] == session_id
        assert (session["profile_id"], session["mode"], session["model"], session["permission_mode"]) == (
            "agent-a", "chat", "sonnet", "default")
        # The owner runs its own binding; nothing is stamped here for it.
        db.set_session_run_binding.assert_not_called()
        db.delete_session.assert_not_called()

    @pytest.mark.parametrize("install, detail", [
        (None, "Unknown device"),
        ({"id": "peer-device-1", "device_name": "Office PC", "online": False}, "Office PC is offline"),
        ({"id": "peer-device-1", "device_name": "Office PC", "online": True, "approved": False}, "waiting for admin approval"),
    ])
    def test_a_runner_that_cannot_take_it_is_refused_before_anything_is_created(
            self, client, db, owner, install, detail):
        db.get_install.return_value = install
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "target_instance_id": self.PEER})

        assert response.status_code == 409
        assert detail in response.json()["detail"]
        db.create_session.assert_not_called()
        owner["create"].assert_not_awaited()

    def test_an_owner_that_refuses_leaves_no_mirror_behind(self, client, db, owner):
        from app.network import control
        owner["create"].side_effect = control.RunnerUnreachable("Couldn't reach Office PC from Home Server.")
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "target_instance_id": self.PEER})

        assert response.status_code == 409
        assert "Couldn't reach Office PC" in response.json()["detail"]
        db.delete_session.assert_called_once_with(_created(db)["session_id"])

    def test_a_lost_answer_is_asked_once_more_and_keeps_the_copy(self, client, db, owner):
        """The owner's create returns an existing session, so asking again is safe."""
        from app.network import control
        owner["create"].side_effect = [control.MaybeApplied("no answer"), {"session_id": "x"}]
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "target_instance_id": self.PEER})

        assert response.status_code == 200, response.text
        assert owner["create"].await_count == 2
        first, second = (c.args[1] for c in owner["create"].await_args_list)
        assert first == second
        db.delete_session.assert_not_called()

    @pytest.mark.parametrize("second", ["MaybeApplied", "RunnerUnreachable"])
    def test_still_no_answer_keeps_the_copy_the_owner_may_have(self, client, db, owner, second):
        from app.network import control
        owner["create"].side_effect = [control.MaybeApplied("no answer"), getattr(control, second)("still nothing")]
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "target_instance_id": self.PEER})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "may or may not have been created on that device" in detail
        # The copy may never sync (the owner may not have created it), so the
        # message never promises it will, and says how to clear it (#706).
        assert "sidebar" not in detail and "syncs" not in detail
        assert "delete it and start a new one" in detail
        db.delete_session.assert_not_called()

    def test_a_refusal_after_a_lost_answer_removes_the_copy(self, client, db, owner):
        from app.network import control
        owner["create"].side_effect = [control.MaybeApplied("no answer"), control.ControlRefused("Unknown profile")]
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "target_instance_id": self.PEER})

        assert response.status_code == 409
        db.delete_session.assert_called_once_with(_created(db)["session_id"])

    def test_a_runner_outside_the_network_leaves_no_copy(self, client, db, owner):
        owner["owner_of"].return_value = None
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "target_instance_id": self.PEER})

        assert response.status_code == 409
        assert "not part of this worker network" in response.json()["detail"]
        db.delete_session.assert_called_once_with(_created(db)["session_id"])
        owner["create"].assert_not_awaited()

    def test_a_preview_cookie_cannot_start_a_chat_on_another_worker(self, client, db, owner, monkeypatch):
        """Mirrors the chat socket (#641): that worker cannot see the preview principal."""
        from types import SimpleNamespace
        from app.core import preview_access
        monkeypatch.setattr(preview_access, "grant", lambda request: SimpleNamespace(session_id="preview"))
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a", "target_instance_id": self.PEER})

        assert response.status_code == 403
        assert response.json()["detail"] == create_api.PREVIEW_ELSEWHERE
        db.create_session.assert_not_called()
        owner["create"].assert_not_awaited()

    def test_a_preview_cookie_still_creates_a_chat_here(self, client, db, monkeypatch):
        from types import SimpleNamespace
        from app.core import preview_access
        monkeypatch.setattr(preview_access, "grant", lambda request: SimpleNamespace(session_id="preview"))
        response = client.post("/api/v1/sessions", json={"profile_id": "agent-a"})

        assert response.status_code == 200
        assert _created(db)["target_instance_id"] is None

    def test_this_worker_as_the_target_runs_here(self, client, db):
        from app.core.config import settings
        response = client.post("/api/v1/sessions", json={
            "profile_id": "agent-a", "target_instance_id": settings.effective_instance_id})

        assert response.status_code == 200
        assert _created(db)["target_instance_id"] is None

    def test_a_legacy_worktree_cannot_move(self, client, db, owner):
        access = {"legacy": {"id": "proj-1", "settings": {}}, "project_v2": {"id": "pv2-1"}, "origin": "git:x"}
        db.get_worktree.return_value = {"id": "wt-1", "repository_id": "repo-1", "branch": "feat/x", "path": "/w/x"}
        db.get_git_repository_by_project.return_value = {"id": "repo-1"}
        with patch.object(create_api, "require_origin_access", return_value=access):
            response = client.post("/api/v1/sessions", json={
                "project_id": "proj-1", "profile_id": "agent-a", "worktree_id": "wt-1",
                "target_instance_id": self.PEER})

        assert response.status_code == 400
        db.create_session.assert_not_called()


class TestPromptlessRunBinding:
    """A promptless with-worktree create stores the composer's picks the way
    the first turn would have; a create with a prompt leaves that to the turn."""

    def test_stores_binding_when_any_pick_is_present(self):
        with patch.object(worktree_api, "database") as db:
            worktree_api.persist_promptless_run_binding(
                "s1", {"model": "opus", "permission_mode": "default", "fast": False, "prompt": "ignored"})
        db.set_session_run_binding.assert_called_once()
        kwargs = db.set_session_run_binding.call_args.kwargs
        assert kwargs["model"] == "opus" and kwargs["fast"] is False
        assert "prompt" not in kwargs

    @pytest.mark.parametrize("overrides", [None, {}, {"system_prompt_append": "x"}])
    def test_skips_when_there_is_nothing_to_store(self, overrides):
        with patch.object(worktree_api, "database") as db:
            worktree_api.persist_promptless_run_binding("s1", overrides)
        db.set_session_run_binding.assert_not_called()

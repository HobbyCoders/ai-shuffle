"""POST /sessions/{id}/attach-project: give a no-project session its project.

Issue #371 step 4. The attach turns the row into an ordinary project session
(mode NULL, project columns set), is one-way, and refuses sessions it must not
touch: one that already has a project or a busy one. A chat another worker runs
is relayed to that worker, which applies the same rules to its own checkout.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api.sessions_attach as attach_api
from app.api.sessions_attach import AttachProjectRequest, attach_project
from app.db import database as db


def _request():
    return Request({"type": "http", "headers": []})


@pytest.fixture
def seeded():
    db.create_profile(profile_id="attach-agent", name="Agent", description=None, config={})
    db.create_project(project_id="attach-proj", name="Web app", description=None, path="/attach-proj")
    db.create_session(session_id="attach-chat", profile_id="attach-agent", mode="chat")
    db.create_session(session_id="attach-code", profile_id="attach-agent", project_id="attach-proj")
    yield


@pytest.fixture
def quiet(monkeypatch):
    """No live run, no pooled client, no sync fan-out, owned here."""
    from app.core import streaming_status

    monkeypatch.setattr(streaming_status, "_has_active_run_db", lambda sid: False)
    pool = MagicMock()
    pool.peek = MagicMock(return_value=None)
    pool._is_awaiting_user_input = MagicMock(return_value=False)
    pool.remove = AsyncMock(return_value=None)
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: pool)
    broadcast = AsyncMock()
    monkeypatch.setattr("app.core.sync_engine.sync_engine.broadcast_user_session_updated", broadcast)
    monkeypatch.setattr(attach_api, "_owned_here", lambda session: True)
    monkeypatch.setattr(attach_api, "get_api_user_from_request", lambda request: None)
    return pool, broadcast


async def _attach(session_id, project_id="attach-proj"):
    session = db.get_session(session_id)
    with patch.object(attach_api, "require_session", AsyncMock(return_value=session)):
        return await attach_project(_request(), session_id, AttachProjectRequest(project_id=project_id), "token")


@pytest.mark.asyncio
async def test_attach_turns_a_no_project_session_into_a_project_session(seeded, quiet):
    pool, broadcast = quiet
    result = await _attach("attach-chat")
    assert result.project_id == "attach-proj"
    assert result.mode == "code"
    row = db.get_session("attach-chat")
    assert row["project_id"] == "attach-proj"
    # Code sessions store NULL (session_modes.storage_session_mode).
    assert row["mode"] is None
    # The live client carried the no-project ceiling; it is closed, not ended.
    pool.remove.assert_awaited_once_with("attach-chat", preserve_session=True)
    broadcast.assert_awaited_once()
    assert broadcast.await_args.kwargs["updates"]["project_id"] == "attach-proj"


@pytest.mark.asyncio
async def test_attach_is_one_way(seeded, quiet):
    await _attach("attach-chat")
    db.create_project(project_id="attach-other", name="Other", description=None, path="/attach-other")
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat", "attach-other")
    assert exc.value.status_code == 409
    assert db.get_session("attach-chat")["project_id"] == "attach-proj"


@pytest.mark.asyncio
async def test_a_project_session_is_refused(seeded, quiet):
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-code")
    assert exc.value.status_code == 409
    assert db.get_session("attach-code")["project_id"] == "attach-proj"


@pytest.mark.asyncio
async def test_a_busy_session_is_refused_without_writing(seeded, quiet, monkeypatch):
    from app.core import streaming_status

    monkeypatch.setattr(streaming_status, "_has_active_run_db", lambda sid: True)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_an_unreadable_busy_state_counts_as_busy(seeded, quiet, monkeypatch):
    from app.core import streaming_status

    def broken(sid):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(streaming_status, "_has_active_run_db", broken)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["mode"] == "chat"


@pytest.mark.asyncio
async def test_a_turn_live_in_this_process_is_busy_even_if_the_database_lags(seeded, quiet, monkeypatch):
    """``has_active_run`` is cached and its lease row may not be written yet;
    the in-memory runtime, executions and pooled client are read first."""
    from app.core import session_runtime

    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: object())
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.detail == attach_api.BUSY

    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: None)
    pool, _ = quiet
    pool.peek.return_value = MagicMock(is_streaming=True, idle_reader_active=False)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_a_turn_queued_after_the_busy_check_blocks_the_write(seeded, quiet, monkeypatch):
    """The write re-checks under turn admission's session lock, so a turn
    admitted after the endpoint's check (here: right before the write) is
    never left running on the old client's ceiling or cut off."""
    from app.db import turns

    real = db.attach_session_project

    def queue_then_attach(*args):
        turns.enqueue_turn("attach-chat", "user", prompt="hi")
        return real(*args)

    monkeypatch.setattr(db, "attach_session_project", queue_then_attach)
    pool, _ = quiet
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.BUSY
    row = db.get_session("attach-chat")
    assert row["mode"] == "chat" and row["project_id"] is None
    pool.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_parked_on_background_work_blocks_and_says_so(seeded, quiet):
    """A parked turn still holds the session (its background work runs on the
    old client), but the list shows it as done: the refusal names background
    work rather than a running turn."""
    from app.db import turns
    from app.db.base import get_db

    turn = turns.enqueue_turn("attach-chat", "user", prompt="hi")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='parked' WHERE id=%s", (turn["id"],))
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.BUSY_BACKGROUND
    assert "background work" in exc.value.detail
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_a_client_that_starts_streaming_after_the_write_is_not_cut_off(seeded, quiet):
    """Checked again right before the release: a turn that started on the
    pooled client after the write keeps it (its own options check rebuilds the
    client for the new cwd), and the attach still lands."""
    pool, _ = quiet
    calls = {"n": 0}

    def peek(sid):
        calls["n"] += 1
        return None if calls["n"] == 1 else MagicMock(is_streaming=True, idle_reader_active=False)

    pool.peek.side_effect = peek
    result = await _attach("attach-chat")
    assert result.project_id == "attach-proj"
    pool.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_row_this_worker_no_longer_runs_is_never_written_here(seeded, quiet, monkeypatch):
    """The relay resolves the owner from the row; if this worker still can't
    claim it (it moved away mid-request), the shared write refuses."""
    monkeypatch.setattr(attach_api, "_owned_here", lambda session: False)
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == 409
    assert exc.value.detail == attach_api.NOT_HERE
    assert db.get_session("attach-chat")["mode"] == "chat"


# =============================================================================
# A chat another worker runs: relayed to that worker
# =============================================================================


@pytest.fixture
def remote_owner(monkeypatch):
    """The chat is run by worker ``dev-b``; record what would be relayed."""
    from app.network import control

    monkeypatch.setattr(control, "owner_of", lambda session: "dev-b")
    sent = AsyncMock(return_value={
        "session_id": "attach-chat", "project_id": "b-local-id", "project_v2_id": "pv2-b",
        "mode": "code", "permission_mode": "default",
    })
    monkeypatch.setattr(control, "call", sent)
    monkeypatch.setattr(attach_api, "_worker_name", AsyncMock(return_value="Worker B"))
    return sent


@pytest.mark.asyncio
async def test_a_remote_chat_is_relayed_by_shared_identity(seeded, quiet, remote_owner):
    from app.core import project_resolver

    pv2 = project_resolver.resolve_project_v2({"project_id": "attach-proj"})
    result = await _attach("attach-chat")
    remote_owner.assert_awaited_once()
    device, action, body = remote_owner.await_args.args
    assert (device, action) == ("dev-b", "attach-project")
    # Legacy ids are per worker: only the shared identity crosses.
    assert body == {"session_id": "attach-chat", "project_v2_id": pv2["id"], "origin": pv2["origin"]}
    assert result.project_id == "b-local-id" and result.permission_mode == "default"
    # Nothing is written here: the owner's row syncs back as for any change.
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_a_remote_only_project_is_relayed_by_its_v2_id(seeded, quiet, remote_owner):
    from app.db import projects_v2

    pv2 = projects_v2.upsert_project("git:example/remote-only", "Remote only")
    session = db.get_session("attach-chat")
    with patch.object(attach_api, "require_session", AsyncMock(return_value=session)):
        await attach_project(_request(), "attach-chat", AttachProjectRequest(project_v2_id=pv2["id"]), "token")
    body = remote_owner.await_args.args[2]
    assert body == {"session_id": "attach-chat", "project_v2_id": pv2["id"], "origin": "git:example/remote-only"}


@pytest.mark.parametrize("failure, status_code, words", [
    ("unreachable", 503, "Worker B runs this chat and must be online"),
    ("maybe", 503, "may still have been applied"),
    ("refused", 409, "Web app isn't checked out on Worker B"),
])
@pytest.mark.asyncio
async def test_relay_failures_read_clearly(seeded, quiet, remote_owner, failure, status_code, words):
    from app.network import control

    remote_owner.side_effect = {
        "unreachable": control.RunnerUnreachable("Couldn't reach Worker B from Worker A."),
        "maybe": control.MaybeApplied("Worker B did not answer in time."),
        "refused": control.ControlRefused("Web app isn't checked out on Worker B, the worker that runs this chat."),
    }[failure]
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat")
    assert exc.value.status_code == status_code
    assert words in exc.value.detail
    if failure == "maybe":
        # Never reported as a clean failure that is safe to retry.
        assert "must be online" not in exc.value.detail


@pytest.mark.asyncio
async def test_row_refusals_are_checked_here_before_relaying(seeded, quiet, remote_owner):
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-code")
    assert exc.value.detail == attach_api.ALREADY_HAS_PROJECT
    remote_owner.assert_not_awaited()


def test_only_the_owner_reaches_the_relay(routed, remote_owner):
    """``require_admin`` and the preview line hold on the viewing worker."""
    from app.core import preview_access

    app, owner_token = routed
    assert _post(_client(app, bearer=MEMBER_PAIRED)).status_code == 403
    preview = {preview_access.cookie_name(): preview_access.mint("agent-session")}
    with patch.object(preview_access, "_session_exists", lambda session_id: True):
        assert _post(_client(app, cookies=preview)).status_code == 403
    remote_owner.assert_not_awaited()
    assert _post(_client(app, bearer=OWNER_PAIRED)).status_code == 200
    remote_owner.assert_awaited_once()


# =============================================================================
# The owner side of the relay: the ``attach-project`` control action
# =============================================================================


@pytest.fixture
def checkout(seeded, tmp_path):
    from app.core import project_resolver

    folder = tmp_path / "web-app"
    folder.mkdir()
    db.create_project(project_id="owner-proj", name="Web app", description=None, path=str(folder))
    return project_resolver.resolve_project_v2({"project_id": "owner-proj"})


@pytest.mark.asyncio
async def test_the_owner_attaches_its_own_checkout(checkout, quiet):
    from app.network import control

    result = await control.handle("attach-project", {
        "session_id": "attach-chat", "project_v2_id": checkout["id"], "origin": checkout["origin"],
        # A viewer's legacy id means nothing here and is ignored.
        "project_id": "attach-proj",
    }, from_device="dev-a")
    assert result["project_id"] == "owner-proj"
    assert result["project_v2_id"] == checkout["id"]
    assert result["permission_mode"] == "default"
    row = db.get_session("attach-chat")
    assert row["project_id"] == "owner-proj" and row["mode"] is None
    assert row["permission_mode"] == "default"
    _, broadcast = quiet
    broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_owner_refuses_a_project_it_has_no_checkout_of(seeded, quiet):
    from app.db import projects_v2
    from app.network import control

    pv2 = projects_v2.upsert_project("git:example/elsewhere", "Elsewhere")
    with pytest.raises(HTTPException) as exc:
        await control.handle("attach-project", {"session_id": "attach-chat", "project_v2_id": pv2["id"]},
                             from_device="dev-a")
    assert exc.value.status_code == 404
    assert exc.value.detail.startswith("Elsewhere isn't checked out on ")
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_the_owner_refuses_a_checkout_whose_folder_is_gone(seeded, quiet):
    from app.core import project_resolver
    from app.network import control

    pv2 = project_resolver.resolve_project_v2({"project_id": "attach-proj"})  # path does not exist
    with pytest.raises(HTTPException) as exc:
        await control.handle("attach-project", {"session_id": "attach-chat", "project_v2_id": pv2["id"]},
                             from_device="dev-a")
    assert exc.value.status_code == 404
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_the_owner_rechecks_busy_under_its_own_state(checkout, quiet, monkeypatch):
    from app.core import streaming_status
    from app.network import control

    monkeypatch.setattr(streaming_status, "_has_active_run_db", lambda sid: True)
    with pytest.raises(HTTPException) as exc:
        await control.handle("attach-project", {"session_id": "attach-chat", "project_v2_id": checkout["id"]},
                             from_device="dev-a")
    assert exc.value.detail == attach_api.BUSY
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.asyncio
async def test_the_owner_needs_a_project_identity(seeded, quiet):
    from app.network import control

    with pytest.raises(ValueError):
        await control.handle("attach-project", {"session_id": "attach-chat", "project_id": "attach-proj"},
                             from_device="dev-a")


@pytest.mark.asyncio
async def test_an_unknown_project_is_refused(seeded, quiet):
    with pytest.raises(HTTPException) as exc:
        await _attach("attach-chat", "no-such-project")
    assert exc.value.status_code == 404
    assert db.get_session("attach-chat")["project_id"] is None


@pytest.mark.parametrize("before", ["bypassPermissions", "auto", "acceptEdits", None])
@pytest.mark.asyncio
async def test_attach_puts_the_session_back_on_ask(seeded, quiet, tmp_path, before):
    """Bypass and Auto were picked for a chat that could not reach a shell.
    After the attach the next turn's options must be Ask, in the project's
    folder, with the project tool set, whatever the old binding was. A NULL
    binding would fall back to the agent's default, here Bypass."""
    import app.core.query_options as qo
    from app.core.session_modes import CHAT_MODE_TOOL_SURFACE

    project_dir = tmp_path / "web-app"
    project_dir.mkdir()
    db.create_project(project_id="attach-real", name="Web app", description=None, path=str(project_dir))
    if before:
        db.set_session_run_binding("attach-chat", permission_mode=before)
        assert db.get_session("attach-chat")["permission_mode"] == before

    result = await _attach("attach-chat", "attach-real")
    assert result.permission_mode == "default"
    row = db.get_session("attach-chat")
    assert row["permission_mode"] == "default"

    with patch("app.core.session_orchestration.is_owner_session", return_value=False), patch.object(
        qo, "get_security_instructions", return_value=""
    ), patch.object(qo, "_rescue_pending_checkout", return_value=False):
        options, _ = qo.build_options_from_profile(
            profile={"id": "attach-agent", "config": {"model": "opus", "permission_mode": "bypassPermissions"}},
            project=db.get_project("attach-real"),
            session_id="attach-chat",
        )
    assert options.permission_mode == "default"
    assert str(options.cwd) == str(project_dir)
    assert not set(options.allowed_tools or []) <= set(CHAT_MODE_TOOL_SURFACE)


def test_the_database_write_refuses_a_row_that_already_has_a_project(seeded):
    assert db.attach_session_project("attach-code", "attach-proj", None) is None
    assert db.attach_session_project("attach-chat", "attach-proj", None)["project_id"] == "attach-proj"
    assert db.attach_session_project("attach-chat", "attach-proj", None) is None


def test_the_route_is_served(seeded, quiet):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.auth import require_admin
    from app.api.sessions import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "token"
    with patch.object(attach_api, "require_session", AsyncMock(return_value=db.get_session("attach-code"))):
        response = TestClient(app).post(
            "/api/v1/sessions/attach-code/attach-project", json={"project_id": "attach-proj"})
    # Reached the handler: refused because it already has a project.
    assert response.status_code == 409
    assert response.json()["detail"] == attach_api.ALREADY_HAS_PROJECT


# =============================================================================
# Who may attach: the owner only
# =============================================================================

OWNER_PAIRED = "shc_owner-device-session"
MEMBER_PAIRED = "shc_member-device-session"


class _Surface:
    """Marks the listener a request arrived on, as ``app.serve`` does."""

    def __init__(self, app, ingress="local"):
        self.app, self.ingress = app, ingress

    async def __call__(self, scope, receive, send):
        scope["shuffle.ingress"] = self.ingress
        await self.app(scope, receive, send)


@pytest.fixture
def routed(seeded, quiet, tmp_path, monkeypatch):
    """The real sessions router behind the real ``require_admin``."""
    from fastapi import FastAPI

    from app.api import auth
    from app.api.sessions import router
    from app.core import owner_auth
    from app.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    paired = {OWNER_PAIRED: {"id": "pc-owner", "account_owner_admin": True},
              MEMBER_PAIRED: {"id": "pc-member", "account_owner_admin": False}}
    monkeypatch.setattr(auth, "get_paired_client_from_token",
                        lambda token, touch=True: dict(paired[token]) if token in paired else None)
    monkeypatch.setattr(attach_api, "require_session",
                        AsyncMock(side_effect=lambda request, sid: db.get_session(sid)))
    app = FastAPI()
    app.include_router(router)
    owner_token, _ = owner_auth.issue_session()
    return app, owner_token


def _client(app, *, host="192.168.1.20:8000", peer="192.168.1.50", bearer=None, cookies=None):
    from fastapi.testclient import TestClient

    base = f"http://{host}"
    headers = {"Origin": base}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    tc = TestClient(_Surface(app), base_url=base, client=(peer, 12345), headers=headers)
    for name, value in (cookies or {}).items():
        tc.cookies.set(name, value)
    return tc


def _post(tc):
    return tc.post("/api/v1/sessions/attach-chat/attach-project", json={"project_id": "attach-proj"})


def _untouched():
    row = db.get_session("attach-chat")
    return row["mode"] == "chat" and row["project_id"] is None


def test_an_api_key_user_is_refused(routed):
    from app.api import auth
    from app.db import api_users
    import os

    app, _ = routed
    key = "aiu_" + os.urandom(16).hex()
    api_users.create_api_user("attach-user", "Integration", auth.hash_api_key(key))
    response = _post(_client(app, bearer=key))
    assert response.status_code == 403
    assert _untouched()


def test_a_non_owner_paired_client_is_refused(routed):
    app, _ = routed
    response = _post(_client(app, bearer=MEMBER_PAIRED))
    assert response.status_code == 403
    assert _untouched()


def test_a_preview_cookie_is_refused_even_alongside_the_owner(routed, monkeypatch):
    from app.core import preview_access

    app, owner_token = routed
    monkeypatch.setattr(preview_access, "_session_exists", lambda session_id: True)
    preview = {preview_access.cookie_name(): preview_access.mint("agent-session")}
    # The agent browser's grant alone, from a LAN peer...
    assert _post(_client(app, cookies=preview)).status_code == 403
    # ...or riding along with an owner cookie on loopback: still refused.
    both = _client(app, host="127.0.0.1:8000", peer="127.0.0.1",
                   cookies={**preview, "session": owner_token})
    assert _post(both).status_code == 403
    assert _untouched()
    # The same owner cookie without the grant is admitted: the grant refused it.
    owner = _client(app, host="127.0.0.1:8000", peer="127.0.0.1", cookies={"session": owner_token})
    assert _post(owner).status_code == 200


def test_the_owner_and_an_owner_paired_phone_may_attach(routed):
    app, owner_token = routed
    response = _post(_client(app, bearer=OWNER_PAIRED))
    assert response.status_code == 200, response.text
    assert response.json()["permission_mode"] == "default"
    assert db.get_session("attach-chat")["project_id"] == "attach-proj"


@pytest.mark.parametrize("mode", [None, "code"])
@pytest.mark.parametrize("permission", [None, "acceptEdits", "bypassPermissions", "auto"])
def test_a_late_legacy_peer_row_can_attach_after_migrations_are_current(
    routed: tuple[Any, str], mode: str | None, permission: str | None,
) -> None:
    """#553 review: startup is complete before the old-peer row is materialized."""
    from app.db.base import get_db
    from app.network import engine, policy

    app, owner_token = routed
    db.init_database()
    fingerprint = db.get_system_setting(db.MIGRATIONS_FINGERPRINT_KEY)
    assert fingerprint == db.migrations_fingerprint()
    incoming = {
        **db.get_session("attach-chat"), "id": "late-owner", "mode": mode,
        "permission_mode": permission,
    }
    assert db.get_session("late-owner") is None
    with get_db() as conn:
        row = engine.materialize(
            conn.cursor(), policy.collection("sessions"), "late-owner", incoming,
        )
        assert row["mode"] == "chat"
    db.add_session_message("late-owner", "user", "preserved peer history")
    assert db.get_session("late-owner")["permission_mode"] == permission

    # A later old-peer edit must not put it back on a stored code surface.
    with get_db() as conn:
        engine.materialize(
            conn.cursor(), policy.collection("sessions"), "late-owner", incoming,
        )
    assert incoming["mode"] == mode  # Local projection never rewrites the peer payload.
    assert db.get_system_setting(db.MIGRATIONS_FINGERPRINT_KEY) == fingerprint
    assert db.get_session("late-owner")["mode"] == "chat"

    client = _client(
        app, host="127.0.0.1:8000", peer="127.0.0.1", cookies={"session": owner_token},
    )
    visible = client.get("/api/v1/sessions/late-owner")
    assert visible.status_code == 200, visible.text
    assert visible.json()["mode"] == "chat"  # NoProjectPill's actual serialized input.
    response = client.post(
        "/api/v1/sessions/late-owner/attach-project", json={"project_id": "attach-proj"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["permission_mode"] == "default"
    attached = db.get_session("late-owner")
    assert attached["project_id"] == "attach-proj"
    assert attached["mode"] is None
    assert attached["permission_mode"] == "default"
    assert db.get_session_messages("late-owner")[0]["content"] == "preserved peer history"

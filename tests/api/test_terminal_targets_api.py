"""Remote targets cannot bypass the terminal UI's owner-only boundary."""
from unittest.mock import AsyncMock

import pytest

from app.core import terminal_targets
from app.core import preview_access
from tests.api.test_terminals_api import ctx, client, owner, MEMBER_PAIRED, OWNER_PAIRED


@pytest.mark.parametrize("target", [{"kind": "worker", "worker_id": "peer"}, {"kind": "ssh", "grant_id": "grant"}])
def test_remote_targets_refuse_agents_nonowners_and_preview(ctx, monkeypatch, target):
    create = AsyncMock()
    monkeypatch.setattr(terminal_targets, "create", create)
    for tc in (client(ctx), client(ctx, bearer="agent-api-key"), client(ctx, bearer=MEMBER_PAIRED)):
        assert tc.post("/api/v1/terminals", json={"target": target}).status_code in (401, 403)
    # A valid preview cookie restricts even a real owner credential.
    tc = owner(ctx)
    monkeypatch.setattr(preview_access, "verify", lambda token: {"session_id": "preview"})
    tc.cookies.set(preview_access.COOKIE_NAME, "fixture-preview")
    assert tc.post("/api/v1/terminals", json={"target": target}).status_code == 403
    create.assert_not_awaited()


def test_owner_discovery_returns_only_public_target_fields(ctx, monkeypatch):
    monkeypatch.setattr(terminal_targets, "targets", AsyncMock(return_value=[
        {"kind": "ssh", "grant_id": "handle", "name": "Approved machine", "available": True}]))
    response = owner(ctx).get("/api/v1/terminals/targets")
    assert response.status_code == 200
    assert response.json()["targets"][-1] == {
        "kind": "ssh", "grant_id": "handle", "name": "Approved machine", "available": True}


def test_mirrored_chat_keeps_explicit_remote_targets(ctx, monkeypatch):
    from app.core import terminals as service

    def remote_folder(*args):
        raise service.TerminalError("chat_on_other_worker", "Chat is remote", 409)

    monkeypatch.setattr(service, "resolve_folder", remote_folder)
    monkeypatch.setattr(terminal_targets, "targets", AsyncMock(return_value=[
        {"kind": "worker", "worker_id": "peer", "name": "Peer", "available": True}]))
    response = owner(ctx).get("/api/v1/terminals/targets?session_id=remote-chat")
    assert response.status_code == 200 and response.json()["supported"]
    assert not response.json()["targets"][0]["available"]
    assert response.json()["default_folder"] is None


def test_owner_open_dispatches_to_selected_remote_target(ctx, monkeypatch):
    from types import SimpleNamespace

    create = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"id": "remote-terminal"}))
    monkeypatch.setattr(terminal_targets, "create", create)
    response = owner(ctx).post("/api/v1/terminals", json={"target": {"kind": "worker", "worker_id": "peer"}})
    assert response.status_code == 201 and response.json()["terminal"]["id"] == "remote-terminal"
    registry, body, who, request = create.await_args.args
    assert registry is ctx.reg and who == "owner" and body.target.worker_id == "peer"
    assert request.cookies["session"] == ctx.owner


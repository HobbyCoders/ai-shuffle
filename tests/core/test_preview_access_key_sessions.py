"""R10-2: a preview grant signs in as the owner, so an API key's session gets none.

The harness put a preview-access URL into every native session's "# Workspace
facts" card, and ``require_auth`` turns a valid grant into ``is_admin=True,
api_user=None``. A key's agent could open it and read unmasked MCP servers,
the owner's usage, every note and every session. Now the harness mints no
token for a session that runs as a key, and ``verify`` refuses any token bound
to one (a worker under a key session included), so the cookie reaches nothing.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import preview_access
from app.core.config import settings
from app.serve import IngressApplication

OWNER = "sess-owner"
KEY = "sess-key"
KEY_WORKER = "sess-key-worker"

_ROWS = {
    OWNER: {"id": OWNER, "api_user_id": None},
    KEY: {"id": KEY, "api_user_id": "key-1"},
    # A Task worker spawned by the key's session runs as the key too.
    KEY_WORKER: {"id": KEY_WORKER, "api_user_id": None, "is_worker": True, "parent_session_id": KEY},
}


@pytest.fixture(autouse=True)
def _rows(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    # The card only quotes a link when the preview listener is up.
    monkeypatch.setattr(preview_access, "_listener_port", 8002)
    from app.db import database, sessions

    lookup = _ROWS.get
    monkeypatch.setattr(sessions, "get_session", lookup)
    monkeypatch.setattr(database, "get_session", lookup)


def test_verify_refuses_tokens_bound_to_key_sessions():
    assert preview_access.verify(preview_access.mint(OWNER)).session_id == OWNER
    assert preview_access.verify(preview_access.mint(KEY)) is None
    assert preview_access.verify(preview_access.mint(KEY_WORKER)) is None


def test_key_session_token_cannot_reach_owner_routes():
    from app.api import auth as auth_api

    api = FastAPI()
    api.include_router(auth_api.router)

    @api.get("/api/v1/mcp/servers")
    async def servers(_: str = Depends(auth_api.require_auth)):
        return {"ok": True}

    client = TestClient(IngressApplication(api, local_port=18091),
                        base_url="http://10.0.0.135:18091", client=("10.0.0.200", 54321))
    # The redirect refuses to set the cookie for a key session ...
    landing = client.get(preview_access.preview_url(KEY, "10.0.0.135", 18091).split("18091", 1)[1],
                         follow_redirects=False)
    assert landing.status_code == 403
    # ... and a cookie planted by hand authenticates as nobody.
    client.cookies.set(preview_access.COOKIE_NAME, preview_access.mint(KEY))
    assert client.get("/api/v1/mcp/servers").status_code == 401
    # The owner's own session still gets its preview.
    client.cookies.set(preview_access.COOKIE_NAME, preview_access.mint(OWNER))
    assert client.get("/api/v1/mcp/servers").json() == {"ok": True}


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("session_id, has_token", [(OWNER, True), (KEY, False), (KEY_WORKER, False)])
def test_workspace_card_carries_no_token_for_key_sessions(monkeypatch, session_id, has_token, delegated):
    """Same rule on both legs: a delegated (Claude CLI) owner session gets its
    preview link too (#1789); an API key's session never does."""
    import app.core.environment as environment
    import app.core.mcp_config as mcp_config
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(mcp_config, "_browser_loopback_host", lambda raw: "10.0.0.135")
    monkeypatch.setattr(environment, "workspace_facts_card",
                        lambda cwd, *, browser_host, app_port, preview_url: f"card preview={preview_url}")
    adapter = ha.HarnessAdapter()
    adapter._is_delegated = delegated
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: session_id)
    card = adapter._build_workspace_card(ha.SessionOptions(model="m", cwd="."))
    assert card.startswith("card preview=")
    assert (preview_access.REDIRECT_PATH in card) is has_token


def test_workspace_card_is_byte_identical_across_reconnects(tmp_path, monkeypatch):
    """#1237: the card sits in the cached system prefix, and an idle-evicted
    client re-renders it when the session comes back from a park. The URL's
    token used to expire at "now + 24h", so every reconnect produced a new
    card and a ``changed=system`` cache miss. Two connects twenty minutes
    apart must now render the same bytes, and the token must still verify."""
    import sys
    from types import SimpleNamespace
    from urllib.parse import unquote

    import app.core.provider.harness_adapter as ha
    from app.core import environment, mcp_config

    root = tmp_path / "ai-shuffle"
    (root / "app").mkdir(parents=True)
    (root / "app" / "preview.py").write_text("# preview")
    (root / "fluid").mkdir()
    monkeypatch.setattr(environment, "_PYTEST_PROBE", {"value": sys.executable})
    monkeypatch.setattr(mcp_config, "_browser_loopback_host", lambda raw: "10.0.0.135")
    # One minute past a six-hour boundary (UTC): the two connects below fall
    # inside the same stable window.
    clock = [1_799_992_860.0]
    monkeypatch.setattr(preview_access, "time", SimpleNamespace(time=lambda: clock[0]))

    def connect():
        adapter = ha.HarnessAdapter()
        monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: OWNER)
        return adapter._build_workspace_card(ha.SessionOptions(model="m", cwd=str(root)))

    first = connect()
    clock[0] += 20 * 60  # parked; the cold client was evicted and reconnects
    second = connect()
    assert preview_access.REDIRECT_PATH in first
    assert first == second

    token = unquote(first.split("token=", 1)[1].split("&", 1)[0])
    grant = preview_access.verify(token)
    assert grant is not None and grant.session_id == OWNER
    lifetime = grant.expires_at - clock[0]
    shortest = preview_access.DEFAULT_TTL - preview_access.STABLE_WINDOW
    assert shortest < lifetime <= preview_access.DEFAULT_TTL


def test_workspace_card_has_no_link_without_the_preview_listener(tmp_path, monkeypatch):
    """The agent browser is refused the worker's own ports, so a link to them
    would be a dead end: with the listener off the card says so instead."""
    import sys

    import app.core.provider.harness_adapter as ha
    from app.core import environment, mcp_config

    root = tmp_path / "ai-shuffle"
    (root / "app").mkdir(parents=True)
    (root / "app" / "preview.py").write_text("# preview")
    (root / "fluid").mkdir()
    monkeypatch.setattr(environment, "_PYTEST_PROBE", {"value": sys.executable})
    monkeypatch.setattr(mcp_config, "_browser_loopback_host", lambda raw: "10.0.0.135")
    monkeypatch.setattr(preview_access, "_listener_port", None)

    adapter = ha.HarnessAdapter()
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: OWNER)
    card = adapter._build_workspace_card(ha.SessionOptions(model="m", cwd=str(root)))
    assert preview_access.REDIRECT_PATH not in card and "token=" not in card
    assert "cannot open the running UI" in card

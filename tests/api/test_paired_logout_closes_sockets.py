"""A paired device's own logout closes its open sockets at once.

``POST /api/v1/auth/logout`` with a paired session clears that session; the
device's sockets would otherwise notice only at their periodic re-check. The
route signals them (``close_paired_client_sockets``), so they close with 4001
now, while the owner's and another paired device's sockets stay open.
Authentication and validation are real (paired-client rows in the test DB).
"""
import asyncio

from fastapi import Response
from starlette.requests import Request

from app.api import auth as auth_routes
from app.api import chat_ws_handler as cwh
from app.api import ws_key_watch
from tests.api import test_ws_paired_device_revocation as revocation
from tests.api.test_ws_paired_device_revocation import paired, ws_env  # noqa: F401 - fixtures

TOKEN_A = revocation.TOKEN_A


def _bearer_request(token):
    return Request({
        "type": "http", "method": "POST", "path": "/api/v1/auth/logout",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 1), "query_string": b"",
    })


async def test_paired_device_logout_closes_its_sockets_immediately(paired):  # noqa: F811
    m = paired.monkeypatch
    # The periodic re-check is far away: only the route's signal can close them.
    m.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 3600.0)
    m.setattr(ws_key_watch, "RECHECK_SECONDS", 3600.0)

    async def _inline(fn, *a, **k):
        return fn(*a, **k)

    m.setattr(auth_routes, "run_sync", _inline)
    m.setattr(auth_routes.audit_service, "log_logout", lambda *a, **k: None)
    sockets = await revocation._open_all()
    try:
        result = await auth_routes.logout(_bearer_request(TOKEN_A), Response())
        assert result["status"] == "ok"
        await revocation._expect_a_closed_others_open(sockets, 1.0)
    finally:
        for wire, task in sockets.values():
            await revocation._hang_up(wire, task)


async def test_owner_logout_does_not_signal_paired_devices(monkeypatch):
    signalled = []

    async def _inline(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(auth_routes, "run_sync", _inline)
    monkeypatch.setattr(auth_routes, "get_paired_client_from_token", lambda token, touch=True: None)
    monkeypatch.setattr(auth_routes.db, "get_auth_session", lambda token: None)
    monkeypatch.setattr(auth_routes.auth_service, "logout", lambda token: None)
    monkeypatch.setattr(ws_key_watch, "close_paired_client_sockets", signalled.append)
    await auth_routes.logout(_bearer_request("owner-token"), Response())
    await asyncio.sleep(0)
    assert signalled == []

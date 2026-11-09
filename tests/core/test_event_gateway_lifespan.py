"""Application shutdown owns the real listener, not only an in-process flag."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, DEFAULT, MagicMock, patch

import pytest

from app.core import event_gateway, event_listener


@pytest.mark.asyncio
@pytest.mark.parametrize("aborted", [False, True])
async def test_application_shutdown_reaps_and_joins_listener(monkeypatch, aborted):
    from app.main import lifespan

    gateway, children = event_gateway.EventGateway(), []
    real_listener = event_listener.NotificationListener

    def start(kwargs):
        child = real_listener(kwargs)
        children.append(child._child)
        return child

    with socket.create_server(("127.0.0.1", 0)) as server:
        kwargs = {"host": "127.0.0.1", "port": server.getsockname()[1], "user": "nobody",
                  "password": "not-a-real-password", "dbname": "nothing", "connect_timeout": 60}
        monkeypatch.setattr(event_gateway, "_gateway", gateway)
        monkeypatch.setattr(event_gateway, "settings", SimpleNamespace(get_database_connection=lambda: kwargs))
        monkeypatch.setattr(event_gateway, "NotificationListener", start)
        try:
            # Unrelated startup services are isolated, but this is the actual
            # app lifespan and a real child blocked in the real driver connect.
            with patch.multiple("app.core.startup", init_core=DEFAULT,
                     init_encryption_and_sessions=DEFAULT, init_workspace=DEFAULT,
                     check_prerequisites=DEFAULT, install_v2_turns_wiring=DEFAULT), \
                 patch("app.core.cluster.register_self", return_value=False), \
                 patch("app.network.startup.initialize"), \
                 patch("app.network.startup.start", AsyncMock(return_value=[])), \
                 patch("app.core.startup_reconcile.reconcile_at_boot", AsyncMock()), \
                 patch("app.core.provider_usage.provider_usage_monitor.stop", AsyncMock()), \
                 patch("app.core.loop_watchdog.start_loop_watchdog"), \
                 patch("app.core.loop_watchdog.stop_loop_watchdog"):
                try:
                    async with lifespan(MagicMock()):
                        gateway._ensure_listener()
                        deadline = asyncio.get_running_loop().time() + 5
                        while not children:
                            assert asyncio.get_running_loop().time() < deadline
                            await asyncio.sleep(.01)
                        assert children[0].poll() is None
                        if aborted:
                            raise RuntimeError("application interrupted")
                except RuntimeError as exc:
                    assert aborted and str(exc) == "application interrupted"
                assert not gateway._thread.is_alive()
                assert children[0].poll() is not None
        finally:
            gateway.stop()  # A failing regression must not leak its child.

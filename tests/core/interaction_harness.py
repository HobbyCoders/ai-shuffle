"""Autouse fixtures shared by the interaction/approval tests.

A plain module: the importing file gets all five by importing them, e.g.
``from tests.core.interaction_harness import (no_connector_delivery,
no_gateway_listener, no_push, reset_backfill_memo_on_setup,
reset_interaction_wait_state)  # noqa: F401``.
"""
import pytest

from app.core import interaction_wait


@pytest.fixture(autouse=True)
def no_gateway_listener(monkeypatch):
    monkeypatch.setattr(interaction_wait, "_ensure_listener", lambda: None)


@pytest.fixture(autouse=True)
def no_push(monkeypatch):
    import app.services.push_notification as pn

    monkeypatch.setattr(pn, "fire_and_forget_notify", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_connector_delivery(monkeypatch):
    """Turn off the best-effort connector-chat funnel of the permission path.

    ``deliver_permission_prompt`` lazily imports ``app.core.connectors.manager``,
    which pulls in every platform SDK (aiogram alone takes seconds). When a
    test is the first in its process to raise a permission prompt, that import
    runs synchronously on the event loop and starves the test's polling
    deadlines, so the test passed only when an earlier test had already paid
    the import. The funnel has its own tests (test_connector_permission_bridge).
    """
    from app.core import connector_permissions

    async def _no_delivery(*_a, **_k):
        return None

    monkeypatch.setattr(connector_permissions, "deliver_permission_prompt", _no_delivery)


@pytest.fixture(autouse=True)
def reset_interaction_wait_state():
    interaction_wait._waiters.clear()
    interaction_wait._listener_registered = False


@pytest.fixture(autouse=True)
def reset_backfill_memo_on_setup():
    """Drop the per-process empty-session memo so each test backfills fresh.

    Unlike ``tests.shared_fixtures.reset_backfill_memo`` this does not reset
    again at teardown.
    """
    from app.core import history_backfill

    history_backfill._reset_memo()

"""Autouse fixtures shared by the interaction/approval tests.

A plain module: the importing file gets all four by importing them, e.g.
``from tests.core.interaction_harness import (no_gateway_listener, no_push,
reset_backfill_memo_on_setup, reset_interaction_wait_state)  # noqa: F401``.
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

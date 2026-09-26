"""Fixtures shared by the API tests (only ones requested by name)."""
import pytest

import app.api.events as events_mod
from app.core.event_gateway import EventGateway


@pytest.fixture
def gw(monkeypatch):
    """Fresh gateway with the PG listener stubbed, wired into the events module."""
    g = EventGateway()
    monkeypatch.setattr(g, "_ensure_listener", lambda: None)
    monkeypatch.setattr(events_mod, "get_gateway", lambda: g)
    return g

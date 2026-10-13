"""The worker runs the memory health check by itself and announces it once."""
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.core import fluid_memory
from app.core.config import settings


def _owner_store_needing_attention():
    store = fluid_memory.store_for_owner("local")
    store.save_memory("The user's editor is Helix.", protected=True)
    for i in range(25):  # sessions happened, but the learner never ran
        store.journal_append({"kind": "user", "text": f"Synthetic request {i}"})
    return store


def test_owner_health_without_a_store_is_healthy_and_creates_nothing():
    view = fluid_memory.owner_health()
    assert view == {"state": "healthy", "checked_at": None, "signals": [], "repairs": []}
    assert not (fluid_memory.skills_root() / "memories.json").exists()


@pytest.mark.asyncio
async def test_sweep_checks_health_and_announces_once(monkeypatch):
    monkeypatch.setattr(settings, "fluid_continual", True)
    _owner_store_needing_attention()
    now = time.time()
    announce = fluid_memory.sweep_memories(now=now)
    assert len(announce) == 1
    owner, store, state = announce[0]
    assert owner == "local" and state["state"] == "attention"
    emit = AsyncMock(return_value={"id": "n1"})
    with patch("app.services.notification_service.emit_notification", emit):
        await fluid_memory._announce_health(owner, store, state)
    emit.assert_awaited_once()
    args, kwargs = emit.await_args
    assert args == ("admin",) and kwargs["category"] == "memory_health"
    assert "Learning has not run" in kwargs["message"] and "Settings, System" in kwargs["message"]
    # Not due again within the hour; when due, the same stretch stays quiet.
    assert fluid_memory.sweep_memories(now=now + 60) == []
    assert fluid_memory.sweep_memories(now=now + 2 * fluid_memory.HEALTH_INTERVAL_SECONDS) == []


def test_curation_makes_the_check_due_early(monkeypatch):
    monkeypatch.setattr(settings, "fluid_continual", False)
    store = _owner_store_needing_attention()
    now = time.time()
    fluid_memory.sweep_memories(now=now)
    assert not fluid_memory.health_due(store, now + 60)
    store.set_last_consolidated_at(now + 30)
    assert fluid_memory.health_due(store, now + 60)


def test_memory_health_api_is_owner_only(client):
    response = client.get("/api/v1/memory/health")
    assert response.status_code in (401, 403)


def test_memory_health_api_returns_the_owner_view(authenticated_client, monkeypatch):
    monkeypatch.setattr(settings, "fluid_continual", True)
    _owner_store_needing_attention()
    fluid_memory.sweep_memories()
    body = authenticated_client.get("/api/v1/memory/health").json()
    assert body["state"] == "attention"
    first = body["signals"][0]
    assert first["state"] == "attention" and first["label"] == "Learning"
    assert {"id", "label", "state", "reason", "detail"} <= set(first)
    assert isinstance(body["repairs"], list) and body["checked_at"]

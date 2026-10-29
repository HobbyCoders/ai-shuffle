"""A peer's replacement credential invalidates only the old local verdict."""
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.core import encryption
from app.db import database as db
from app.network import engine, policy
from tests.core.network.harness import Worker, exchange, join


@pytest.fixture(autouse=True)
def data_keys(monkeypatch):
    original = Worker.active
    keys = {}

    @contextmanager
    def active(worker):
        key = keys.setdefault(worker.name, encryption.Fernet.generate_key())
        with patch.object(encryption, "_encryption_key", key), original(worker):
            yield worker

    monkeypatch.setattr(Worker, "active", active)


def pair(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Verdicts")
        db.create_model_provider("api", "API", "custom", "https://example.test/v1",
                                 auth_token=encryption.encrypt_value("old-key"),
                                 models=[{"id": "model"}])
        engine.scan_collection("model_providers")
    join(a, b)
    with b.active():
        db.record_credential_check("api", False)
        assert db.list_usable_model_providers() == []
    return a, b


@pytest.mark.parametrize("change", ["auth_token", "base_url", "provider_type", "auth_mode"])
def test_incoming_credential_change_clears_rejection(workers, change):
    a, b = pair(workers)
    with a.active():
        value = {"auth_token": encryption.encrypt_value("new-key"),
                 "base_url": "https://replacement.test/v1", "provider_type": "openai",
                 "auth_mode": "api_key"}[change]
        db.update_model_provider("api", **{change: value})
        assert engine.scan_collection("model_providers") == 1
    exchange(a, b)
    with b.active():
        assert b.sql("SELECT credential_status FROM model_providers WHERE id='api'")[0]["credential_status"] is None
        assert [r["id"] for r in db.list_usable_model_providers()] == ["api"]
        assert engine.scan_collection("model_providers") == 0


@pytest.mark.parametrize("edit", ["name", "same-key", "same-endpoint", "enabled"])
def test_unrelated_edit_keeps_local_rejection(workers, edit):
    a, b = pair(workers)
    with a.active():
        values = {"name": {"name": "Renamed"},
                  "same-key": {"name": "Rekeyed", "auth_token": encryption.encrypt_value("old-key")},
                  "same-endpoint": {"name": "Unchanged endpoint", "base_url": "https://example.test/v1"},
                  "enabled": {"enabled": False}}[edit]
        db.update_model_provider("api", **values)
        engine.scan_collection("model_providers")
    exchange(a, b)
    assert b.sql("SELECT credential_status FROM model_providers WHERE id='api'")[0]["credential_status"] == "rejected"


def test_partial_record_preserves_verdict(workers):
    _, b = pair(workers)
    with b.active():
        row = engine.materialize(b.shim.cursor(), policy.collection("model_providers"),
                                 "api", {"name": "Metadata only", "provider_type": "custom",
                                         "base_url": "https://example.test/v1"})
        assert row["credential_status"] == "rejected"

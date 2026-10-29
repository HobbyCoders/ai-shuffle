"""Provider edits wake the post-commit scanner; local observations do not."""
from unittest.mock import patch

import pytest

from app.core import resource_sync
from app.db import database, model_providers
from app.network import startup, transport


SHARED_WRITERS = (
    "create_model_provider", "update_model_provider", "delete_model_provider",
    "set_model_provider_enabled", "set_codex_token_bundle",
)


@pytest.mark.parametrize("name", SHARED_WRITERS)
def test_shared_provider_writer_registration(name):
    writer = getattr(model_providers, name)
    assert writer.resource_sync == ("settings", "admin")
    assert getattr(database, name) is writer


@pytest.mark.parametrize("writer", [
    model_providers.record_credential_check,
    model_providers.try_claim_codex_refresh,
    model_providers.release_codex_refresh_claim,
])
def test_local_provider_writers_do_not_request_publication(writer):
    assert not getattr(writer, "resource_sync", None)


def test_real_provider_commits_request_immediate_scan(workers, monkeypatch):
    worker = workers("provider-writes")
    requests = []
    monkeypatch.setattr(resource_sync, "_emit", lambda *args: None)
    monkeypatch.setattr(resource_sync, "_network_hooked", False, raising=False)
    monkeypatch.setattr(transport, "request_scan", lambda names, **kw: requests.append(names))
    startup._install_resource_hook()

    def committed(action):
        requests.clear()
        assert action()
        assert len(requests) == 1
        assert "model_providers" in requests[0]

    with worker.active():
        committed(lambda: database.create_model_provider(
            "provider", "API", "openai", "https://api.openai.com/v1"))
        committed(lambda: database.update_model_provider("provider", name="Renamed"))
        committed(lambda: database.set_model_provider_enabled("provider", False))
        committed(lambda: database.set_model_provider_enabled("provider", True))
        committed(lambda: database.set_codex_token_bundle(
            "provider", access_token=None, refresh_token=None, account_id="account"))
        requests.clear()
        database.record_credential_check("provider", False)
        database.release_codex_refresh_claim("provider", "local-owner")
        assert requests == []
        committed(lambda: database.delete_model_provider("provider"))
        requests.clear()
        assert not database.set_model_provider_enabled("missing", False)
        assert not database.delete_model_provider("missing")
        assert requests == []


def test_provider_commit_failure_does_not_request_scan(workers, monkeypatch):
    worker = workers("failed-provider-write")
    with worker.active(), patch.object(resource_sync, "_emit") as emit:
        with patch.object(worker.shim, "commit", side_effect=RuntimeError("commit rejected")):
            with pytest.raises(RuntimeError, match="commit rejected"):
                database.create_model_provider("failed", "API", "custom", "https://example.test/v1")
        emit.assert_not_called()

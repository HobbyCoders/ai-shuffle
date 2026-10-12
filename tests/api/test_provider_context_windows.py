"""Context evidence survives discovery, curation and native session reporting."""
from types import SimpleNamespace

import httpx
import pytest

from app.api import model_providers as api
from app.core import model_registry
from app.db import database as db
from tests.api.test_fetch_models_api import (
    ROOT, PROVIDER_ID, _make_provider, _install_mock_transport,
)


CASES = [
    ("openrouter", "deepseek/deepseek-v4.1-flash", 1_048_576),
    ("openrouter_account", "meta-llama/llama-3.3-70b-instruct", 131_072),
    ("xai", "grok-4.6", 500_000),
]


@pytest.mark.parametrize("kind,model,window", CASES)
def test_discover_add_and_preserve_window(authenticated_client, mock_db, monkeypatch, kind, model, window):
    _make_provider(provider_type=kind)
    upstream = {"id": model}
    if kind != "xai":
        upstream["top_provider"] = {"context_length": window}
    _install_mock_transport(monkeypatch, lambda _: httpx.Response(200, json={"data": [upstream]}))
    response = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
    assert response.status_code == 200
    models = response.json()["models"]
    assert len(models) == 1
    assert models[0]["context_window"] == window
    assert db.get_model_provider(PROVIDER_ID)["models_json"] == []
    saved = authenticated_client.patch(f"{ROOT}/{PROVIDER_ID}", json={"models": models})
    assert saved.status_code == 200
    row = db.get_model_provider(PROVIDER_ID)
    assert len(row["models_json"]) == 1
    assert row["models_json"][0]["context_window"] == window
    assert model_registry.resolve_provider_context_window(row, model) == window
    assert saved.json()["models"][0]["context_window"] == window


@pytest.mark.parametrize("route", ["fetch-models", "refresh-capabilities"])
@pytest.mark.parametrize("kind,model,window", CASES)
def test_refresh_backfills_without_changing_curation_or_owner_edits(
    authenticated_client, mock_db, monkeypatch, route, kind, model, window,
):
    _make_provider(provider_type=kind)
    entries = [{"id": model, "label": "Mine"},
               {"id": "owner-edited", "label": "Keep", "context_window": 12345},
               {"id": "unknown", "label": "Unknown"}]
    db.update_model_provider(PROVIDER_ID, models_json=entries, default_model=model, context_window=9876)
    payload = [{"id": model, "context_length": window},
               {"id": "owner-edited", "context_length": 999999},
               {"id": "not-curated", "context_length": 55555}]
    _install_mock_transport(monkeypatch, lambda _: httpx.Response(200, json={"data": payload}))
    api._capability_results.clear()
    api._capability_locks.clear()
    try:
        url = f"{ROOT}/{PROVIDER_ID}/fetch-models" if route == "fetch-models" else f"{ROOT}/refresh-capabilities"
        response = authenticated_client.post(url, json={"provider_ids": [PROVIDER_ID]})
        assert response.status_code == 200
        if route == "refresh-capabilities":
            assert response.json() == {"refreshed": [PROVIDER_ID], "failed": []}
        row = db.get_model_provider(PROVIDER_ID)
        assert len(row["models_json"]) == 3
        assert [(e["id"], e["label"]) for e in row["models_json"]] == [(e["id"], e["label"]) for e in entries]
        assert [e.get("context_window") for e in row["models_json"]] == [window, 12345, None]
        assert row["default_model"] == model
        assert row["context_window"] == 9876
    finally:
        api._capability_results.clear()
        api._capability_locks.clear()


@pytest.mark.parametrize("race_at", ["network", "write"])
def test_discovery_backfill_rejects_concurrent_edit(authenticated_client, mock_db, monkeypatch, race_at):
    _make_provider()
    db.update_model_provider(PROVIDER_ID, models_json=[{"id": "model", "label": "Old"}])
    update = db.update_model_provider
    edited = [{"id": "model", "label": "Owner", "context_window": 12345}]

    def handler(_):
        if race_at == "network":
            update(PROVIDER_ID, models_json=edited)
        return httpx.Response(200, json={"data": [{"id": "model", "context_length": 99999}]})

    def racing_update(provider_id, **kwargs):
        update(provider_id, models_json=edited)
        return update(provider_id, **kwargs)

    _install_mock_transport(monkeypatch, handler)
    if race_at == "write":
        monkeypatch.setattr(db, "update_model_provider", racing_update)
    response = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
    assert response.status_code == 200
    stored = db.get_model_provider(PROVIDER_ID)["models_json"]
    assert len(stored) == 1
    assert (stored[0]["label"], stored[0]["context_window"]) == ("Owner", 12345)


@pytest.mark.parametrize("kind,model,window", CASES)
@pytest.mark.parametrize("source", ["model", "preset", "row", "unknown"])
def test_resolver_all_provider_types(kind, model, window, source):
    entry = {"id": model}
    row = {"provider_type": kind, "models_json": [entry]}
    if source == "model":
        entry["context_window"] = 12345
        row["context_window"] = 9876
        expected = 12345
    elif source == "preset":
        row["context_window"] = 9876
        expected = window if kind == "xai" else 9876
    else:
        model = "unknown-model"
        row["models_json"] = [{"id": model}]
        row["context_window"] = 9876 if source == "row" else None
        expected = row["context_window"]
    assert model_registry.resolve_provider_context_window(row, model) == expected
    assert model_registry.resolve_provider_context_window(row, model, row["models_json"][0]) == expected


@pytest.mark.parametrize("method", ["post", "patch"])
@pytest.mark.parametrize("own,expected", [(None, 500_000), (12345, 12345)])
def test_save_typed_grok_model_captures_preset(authenticated_client, mock_db, method, own, expected):
    model = {"id": "grok-4.6", "label": "My Grok"}
    if own is not None:
        model["context_window"] = own
    if method == "post":
        response = authenticated_client.post(ROOT, json={
            "name": "xAI", "provider_type": "xai", "base_url": "https://api.x.ai/v1",
            "models": [model],
        })
    else:
        _make_provider(provider_type="xai")
        response = authenticated_client.patch(f"{ROOT}/{PROVIDER_ID}", json={"models": [model]})
    assert response.status_code == 200
    row = db.get_model_provider(response.json()["id"])
    assert len(row["models_json"]) == 1
    assert row["models_json"][0]["context_window"] == expected


def test_preset_before_upstream_and_no_row_value_persisted():
    row = {"provider_type": "xai", "context_window": 9876}
    result = api._with_context_windows(row, [{"id": "grok-4.6"}, {"id": "unknown"}],
                                       [{"id": "grok-4.6", "context_window": 12345}])
    assert result == [{"id": "grok-4.6", "context_window": 500_000}, {"id": "unknown"}]


@pytest.mark.parametrize("kind,model,window", CASES + [("openrouter", "unknown-model", None)])
@pytest.mark.asyncio
async def test_discovered_window_reaches_session_payload(
    authenticated_client, mock_db, monkeypatch, tmp_path, kind, model, window,
):
    from unittest.mock import AsyncMock
    from app.api import sessions
    from app.core import session_pool
    from app.core.config import settings
    from app.core.provider import harness_adapter as ha
    from app.core.provider.types import SessionOptions
    from fluid.store.base import MemoryStore
    from tests.test_harness_adapter import FakeProvider
    import fluid.context.plugins as plugins

    _make_provider(provider_type=kind)
    upstream = {"id": model}
    if kind != "xai":
        upstream["context_length"] = window
    _install_mock_transport(monkeypatch, lambda _: httpx.Response(200, json={"data": [upstream]}))
    discovered = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
    assert discovered.status_code == 200
    saved = authenticated_client.patch(f"{ROOT}/{PROVIDER_ID}", json={"models": discovered.json()["models"]})
    assert saved.status_code == 200

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(plugins, "discover_plugins", lambda *a, **kw: [])
    monkeypatch.setattr(ha, "_build_provider_client", AsyncMock(return_value=(FakeProvider([]), None)))
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _: MemoryStore()))
    adapter = ha.HarnessAdapter()
    try:
        await adapter.connect(SessionOptions(model=model, cwd=str(tmp_path), extra={
            "provider_id": PROVIDER_ID, "fluid_provider": "openrouter", "fluid_engine": "native",
        }))
        state = SimpleNamespace(is_connected=True, adapter=adapter)
        monkeypatch.setattr(session_pool, "get_session_pool", lambda: SimpleNamespace(peek=lambda _: state))
        monkeypatch.setattr(sessions, "require_session", AsyncMock())
        payload = await sessions.get_session_context_usage(None, "test-session", token="test")
        assert payload["available"] is True
        assert payload["source"] == "live"
        assert payload["usage"]["maxTokens"] == window
    finally:
        await adapter.disconnect()

"""Subscription generations merge independently of provider row LWW (#207)."""
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx

import pytest

from app.api import model_providers as provider_api
from app.core import codex_oauth, codex_token_refresh, encryption, xai_oauth, xai_token_refresh
from app.db import database, model_providers
from app.db import network as store
from app.network import engine, refresh_authority
from tests.core.network.harness import converge, exchange, join
from tests.fakes.http import mock_transport_client

PROVIDER = "subscription"
OLD = "2026-10-18T12:00:00+00:00"
NEW = "2026-10-19T12:00:00+00:00"


@pytest.fixture(autouse=True)
def credential_key(monkeypatch):
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())


def rotate(stamp, generation, mode="chatgpt", needs_reauth=False):
    # Exercise the actual producer, not only a seeded sync payload.
    model_providers.set_codex_token_bundle(
        PROVIDER, access_token=encryption.encrypt_value("access-" + generation),
        refresh_token=encryption.encrypt_value("refresh-" + generation),
        account_id="account-" + generation, id_token_expires_at=stamp,
        last_refresh=stamp, needs_reauth=needs_reauth, auth_mode=mode)


def scan():
    return engine.scan_all(include_catalog=False, collections=["model_providers"])


def setup_pair(workers, mode="chatgpt", stamp=OLD):
    renewer, editor = workers("renewer"), workers("editor")
    with renewer.active():
        engine.create_network("Credential merge")
        database.create_model_provider(PROVIDER, "Original", "openai", "https://example.com")
        rotate(stamp, "old", mode, needs_reauth=True)
        if stamp is None:
            # Only legacy writers produced unversioned bundles. New writes
            # always have a generation, even if the caller omitted its clock.
            with database.get_db() as conn:
                conn.cursor().execute("UPDATE model_providers SET codex_last_refresh = NULL WHERE id = %s", (PROVIDER,))
        scan()
    join(renewer, editor)
    converge([renewer, editor])
    return renewer, editor


def edit_later(worker):
    with worker.active():
        assert refresh_authority.authority_device_id(engine.context()) != worker.device_id
        # Simulate unrelated Lamport activity while disconnected, then a
        # normal settings/model-catalog edit carrying the stale bundle.
        with database.get_db() as conn:
            store.observe_clock(conn.cursor(), 10000)
        database.update_model_provider(PROVIDER, name="Edited", enabled=False,
                                       models_json=[{"id": "new-model", "label": "New model"}])
        assert scan() == 1


def assert_bundle(worker, generation="new", stamp=NEW, mode="chatgpt"):
    with worker.active():
        bundle = model_providers.get_codex_tokens(PROVIDER)
        assert encryption.decrypt_value(bundle["codex_access_token"]) == "access-" + generation
        assert encryption.decrypt_value(bundle["codex_refresh_token"]) == "refresh-" + generation
        assert bundle["codex_account_id"] == "account-" + generation
        assert str(bundle["codex_last_refresh"]) == stamp
        assert str(bundle["codex_id_token_expires_at"]) == stamp
        assert bundle["auth_mode"] == mode
        assert bool(bundle["needs_reauth"]) == (generation == "old")


def assert_edit(worker):
    with worker.active():
        row = database.get_model_provider(PROVIDER)
        assert row["name"] == "Edited"
        assert not row["enabled"]
        assert row["models_json"][0]["id"] == "new-model"


@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
@pytest.mark.parametrize("delivery", ["stale-first", "rotation-first"])
def test_concurrent_edit_and_rotation_converge(workers, mode, delivery):
    renewer, editor = setup_pair(workers, mode)
    with renewer.active():
        rotate(NEW, "new", mode)
        assert scan() == 1
    edit_later(editor)
    pairs = [(editor, renewer), (renewer, editor)]
    if delivery == "rotation-first":
        pairs.reverse()
    for sender, receiver in pairs:
        result = exchange(sender, receiver)
        assert result.rejected == result.retry == result.pending == 0, result.reasons
    for worker in (renewer, editor):
        assert_bundle(worker, mode=mode)
        assert_edit(worker)
        with worker.active():
            scan()  # Publish the merged row, not a perpetual local echo.
    converge([renewer, editor])
    for worker in (renewer, editor):
        assert_bundle(worker, mode=mode)
        assert_edit(worker)
        with worker.active():
            assert scan() == 0
    # A new worker bootstraps the merged result, not the stale signed op.
    fresh = workers("fresh")
    join(editor, fresh)
    assert_bundle(fresh, mode=mode)
    assert_edit(fresh)


@pytest.mark.parametrize("newer_row_wins", [False, True])
def test_snapshot_merges_credentials_in_both_clock_orders(workers, newer_row_wins):
    renewer, editor = setup_pair(workers)
    with renewer.active():
        rotate(NEW, "new")
        scan()
    edit_later(editor)
    sender, receiver = (editor, renewer) if newer_row_wins else (renewer, editor)
    with sender.active():
        chunk = engine.snapshot_chunk("model_providers", 0)
    with receiver.active():
        engine.apply_snapshot_chunk(chunk)
    assert_bundle(receiver)
    assert_edit(receiver)


def test_unscanned_rotation_survives_and_is_published(workers):
    renewer, editor = setup_pair(workers)
    with renewer.active():
        rotate(NEW, "new")  # A refresh committed before the periodic scanner.
    edit_later(editor)
    result = exchange(editor, renewer)
    assert result.retry == result.rejected == 0
    assert_bundle(renewer)
    with renewer.active():
        assert scan() == 1
    exchange(renewer, editor)
    assert_bundle(editor)
    assert_edit(editor)


def test_legacy_unversioned_bundle_cannot_replace_rotated_credentials(workers):
    renewer, editor = setup_pair(workers, stamp=None)
    with renewer.active():
        rotate(NEW, "new")
        scan()
    edit_later(editor)
    exchange(editor, renewer)
    exchange(renewer, editor)
    for worker in (renewer, editor):
        assert_bundle(worker)
        assert_edit(worker)


def test_rolling_upgrade_old_receiver_still_needs_upgrade(workers):
    renewer, old, stale = *setup_pair(workers), workers("stale")
    join(renewer, stale)
    with renewer.active():
        rotate(NEW, "new")
        scan()
    exchange(renewer, old)
    assert_bundle(old)
    edit_later(stale)
    # Old releases have identical wire fields, but their UPSERT has no
    # per-bundle guard. Model that receiver, without changing the producer.
    with patch.object(engine, "_PROVIDER_CREDENTIAL_FIELDS", ()):
        exchange(stale, old)
    assert_bundle(old, "old", OLD)
    # Its legacy whole-row record can be forwarded; the upgraded renewer
    # keeps its generation even though the record has a higher row clock.
    exchange(old, renewer)
    assert_bundle(renewer)
    assert_edit(renewer)
    with renewer.active():
        scan()
    exchange(renewer, old)
    assert_bundle(old)


@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
async def test_equal_generation_reauth_status_still_uses_row_lww(workers, mode):
    renewer, editor = setup_pair(workers, mode)
    refresh = codex_token_refresh if mode == "chatgpt" else xai_token_refresh
    clear = provider_api.codex_clear_reauth if mode == "chatgpt" else provider_api.xai_clear_reauth
    with renewer.active():
        await clear(PROVIDER, token="test")
        assert str(model_providers.get_codex_tokens(PROVIDER)["codex_last_refresh"]) == OLD
        scan()
    exchange(renewer, editor)
    with editor.active():
        assert not model_providers.get_codex_tokens(PROVIDER)["needs_reauth"]
    with renewer.active():
        await refresh._mark_needs_reauth(PROVIDER)
        assert str(model_providers.get_codex_tokens(PROVIDER)["codex_last_refresh"]) == OLD
        scan()
    exchange(renewer, editor)
    with editor.active():
        assert model_providers.get_codex_tokens(PROVIDER)["needs_reauth"]


def test_losing_rotation_does_not_revive_deleted_provider(workers):
    renewer, editor = setup_pair(workers)
    with renewer.active():
        rotate(NEW, "new")
        scan()
    with editor.active():
        with database.get_db() as conn:
            store.observe_clock(conn.cursor(), 10000)
        database.delete_model_provider(PROVIDER)
        scan()
    exchange(renewer, editor)
    with editor.active():
        assert database.get_model_provider(PROVIDER) is None

@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
async def test_reconnect_with_clock_behind_wins_on_every_worker(workers, monkeypatch, mode):
    # The old renewer's clock was far ahead of the signing-in worker's clock.
    prior = "2099-10-19T12:00:00+00:00"
    expected = "2099-10-19T12:00:00.000001+00:00"
    renewer, editor = setup_pair(workers, mode, stamp=prior)
    oauth = codex_oauth if mode == "chatgpt" else xai_oauth
    monkeypatch.setattr(oauth, "poll_device_code", AsyncMock(return_value=oauth.TokenBundle(
        access_token="access-new", refresh_token="refresh-new", account_id="account-new")))
    if mode == "chatgpt":
        pending, poll = provider_api._pending_enrollments, provider_api.codex_device_poll
        monkeypatch.setitem(pending, PROVIDER, ("device", "verifier", "code", 0))
    else:
        pending, poll = provider_api._pending_xai_enrollments, provider_api.xai_device_poll
        monkeypatch.setitem(pending, PROVIDER, ("device", 0))
    with editor.active():
        assert (await poll(PROVIDER, token="test")).status == "complete"
        assert scan() == 1
    with renewer.active():
        # A disconnected, unrelated edit wins whole-row LWW while still
        # carrying the dead bundle. Exercise both guarded merge directions.
        with database.get_db() as conn:
            store.observe_clock(conn.cursor(), 10000)
        database.update_model_provider(PROVIDER, name="Edited")
        assert scan() == 1
    exchange(renewer, editor)
    exchange(editor, renewer)
    for worker in (renewer, editor):
        with worker.active():
            assert scan() == 1  # publish the combined row once
    converge([renewer, editor])
    for worker in (renewer, editor):
        with worker.active():
            bundle = model_providers.get_codex_tokens(PROVIDER)
            assert encryption.decrypt_value(bundle["codex_refresh_token"]) == "refresh-new"
            assert str(bundle["codex_last_refresh"]) == expected
            assert not bundle["needs_reauth"]
            assert scan() == 0


@pytest.mark.parametrize("mode", ["chatgpt", "grok"])
async def test_new_renewer_with_clock_behind_rotates_everywhere(workers, monkeypatch, mode):
    founder, peer = setup_pair(workers, mode, stamp=NEW)
    clear = provider_api.codex_clear_reauth if mode == "chatgpt" else provider_api.xai_clear_reauth
    refresh = codex_token_refresh if mode == "chatgpt" else xai_token_refresh
    with founder.active():
        await clear(PROVIDER, token="test")
        scan()
    converge([founder, peer])
    observer = workers("observer")
    join(peer, observer)
    with peer.active():
        engine.revoke_device(founder.device_id)
    converge([peer, observer])
    renewer, observer = sorted([peer, observer], key=lambda worker: worker.device_id)
    monkeypatch.setattr(refresh, "_now", lambda: datetime.fromisoformat(OLD))
    with renewer.active():
        assert refresh_authority.authority_device_id(engine.context()) == renewer.device_id
        async with mock_transport_client(lambda _: httpx.Response(200, json={
            "access_token": "access-new", "refresh_token": "refresh-new",
        })) as client:
            assert await refresh.refresh_provider(PROVIDER, client=client, force=True)
        assert scan() == 1
    exchange(renewer, observer)
    converge([renewer, observer])
    for worker in (renewer, observer):
        with worker.active():
            bundle = model_providers.get_codex_tokens(PROVIDER)
            assert encryption.decrypt_value(bundle["codex_refresh_token"]) == "refresh-new"
            assert str(bundle["codex_last_refresh"]) == "2026-10-19T12:00:00.000001+00:00"
            assert not bundle["needs_reauth"]
            assert scan() == 0

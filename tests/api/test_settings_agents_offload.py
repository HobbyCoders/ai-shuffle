"""The agents / settings endpoints must not do blocking DB work on the loop.

Every synchronous ``database.*`` call made straight from an ``async def``
handler freezes EVERY other request, WebSocket frame and SSE stream served by
the same worker for its whole duration. These endpoints were the densest
remaining cluster: ``GET /api/v1/settings/integrations`` alone made 21 blocking
round trips, and the profile endpoints are 1 + N (one query per enabled
subagent).

Like ``test_chat_ws_offload.py``, these tests pin the fix by identity rather
than wire shape: each spy records the thread its call actually ran on, and the
assertions require (a) that no call touched the loop thread and (b) that the
calls of one handler shared a SINGLE worker thread — i.e. one offload hop, not
one hop per query.
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.api.agents as agents_api
import app.api.settings as settings_api
import app.api.settings_audio as audio_api
import app.api.settings_credentials as creds_api
import app.api.settings_media as media_api


class _ThreadSpy:
    """Wrap a callable, recording the thread each call ran on."""

    def __init__(self, fn):
        self._fn = fn
        self.threads = []

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        return self._fn(*args, **kwargs)


@pytest.fixture
def loop_thread():
    """The thread the test's event loop runs on — nothing blocking may run here."""
    return threading.get_ident()


def _request(api_user=None):
    """The bare shape ``get_api_user_from_request`` reads off a Request."""
    return SimpleNamespace(state=SimpleNamespace(api_user=api_user))


def _assert_one_worker_hop(loop_thread, *spies):
    threads = []
    for spy in spies:
        assert spy.threads, "a spied DB call never ran"
        threads += spy.threads
    assert loop_thread not in threads, "a DB call blocked the event loop"
    assert len(set(threads)) == 1, f"expected ONE worker thread, saw {set(threads)}"


# ═══ agents.py — the profile endpoints ══════════════════════════════════════


async def test_list_profiles_reads_off_the_loop_in_one_hop(mock_db, loop_thread):
    """The count aggregate and the profile listing share one hop."""
    counts = _ThreadSpy(agents_api.database.get_all_profile_session_counts)
    listing = _ThreadSpy(agents_api.database.get_all_profiles)

    with patch.object(agents_api.database, "get_all_profile_session_counts", counts), \
         patch.object(agents_api.database, "get_all_profiles", listing):
        profiles = await agents_api.list_profiles(_request(), token="t")

    assert any(p["id"] == "default" for p in profiles), "the seeded profile is still returned"
    _assert_one_worker_hop(loop_thread, counts, listing)


async def test_list_profiles_for_a_restricted_api_user_is_still_one_hop(
    mock_db, loop_thread
):
    """An API user with N assigned profiles was 1 + N blocking queries."""
    counts = _ThreadSpy(agents_api.database.get_all_profile_session_counts)
    get_profile = _ThreadSpy(agents_api.database.get_profile)

    with patch.object(agents_api.database, "get_all_profile_session_counts", counts), \
         patch.object(agents_api.database, "get_profile", get_profile):
        profiles = await agents_api.list_profiles(
            _request({"profile_ids": ["default", "default", "default"]}), token="t"
        )

    assert [p["id"] for p in profiles] == ["default"] * 3
    assert len(get_profile.threads) == 3, "every assigned profile is still read"
    _assert_one_worker_hop(loop_thread, counts, get_profile)


async def test_get_profile_reads_row_and_count_off_the_loop(mock_db, loop_thread):
    row = _ThreadSpy(agents_api.database.get_profile)
    count = _ThreadSpy(agents_api.database.get_profile_session_count)

    with patch.object(agents_api.database, "get_profile", row), \
         patch.object(agents_api.database, "get_profile_session_count", count):
        profile = await agents_api.get_profile(_request(), "default", token="t")

    assert profile["id"] == "default"
    assert profile["session_count"] == 0
    _assert_one_worker_hop(loop_thread, row, count)


async def test_get_profile_still_404s_from_inside_the_offload(mock_db):
    """An HTTPException raised in the worker thread must propagate unchanged."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await agents_api.get_profile(_request(), "no-such-profile", token="t")
    assert exc.value.status_code == 404


async def test_get_profile_agents_fans_out_in_one_hop(mock_db, loop_thread):
    """1 + N: the profile row plus one read per enabled subagent."""
    now = "2026-08-28T00:00:00+00:00"

    def _subagent(agent_id):
        return {
            "id": agent_id, "name": agent_id, "description": "d", "prompt": "p",
            "tools": None, "model": None, "skills": None, "memory": None,
            "mcp_servers": None, "is_builtin": False,
            "created_at": now, "updated_at": now,
        }

    row = _ThreadSpy(
        lambda pid: {"id": pid, "config": {"enabled_agents": ["a", "b", "c"]}}
    )
    sub = _ThreadSpy(_subagent)

    with patch.object(agents_api.database, "get_profile", row), \
         patch.object(agents_api.database, "get_subagent", sub):
        agents = await agents_api.get_profile_agents(_request(), "default", token="t")

    assert [a.id for a in agents] == ["a", "b", "c"]
    _assert_one_worker_hop(loop_thread, row, sub)


async def test_create_profile_agent_keeps_read_decide_write_in_one_hop(
    mock_db, loop_thread
):
    """The 'does this subagent id already exist' probe decides whether the
    insert happens, and the enabled-agents list is read-modified-written. All of
    it must stay inside a SINGLE offload or the check/insert gap becomes a race
    that did not exist before."""
    now = "2026-08-28T00:00:00+00:00"
    created = {
        "id": "new-agent", "name": "New Agent", "description": "d", "prompt": "p",
        "tools": None, "model": None, "skills": None, "memory": None,
        "mcp_servers": None, "is_builtin": False,
        "created_at": now, "updated_at": now,
    }

    row = _ThreadSpy(lambda pid: {"id": pid, "config": {"enabled_agents": []}})
    probe = _ThreadSpy(lambda _id: None)          # nothing there yet
    insert = _ThreadSpy(lambda **kw: created)
    update = _ThreadSpy(lambda **kw: None)

    with patch.object(agents_api.database, "get_profile", row), \
         patch.object(agents_api.database, "get_subagent", probe), \
         patch.object(agents_api.database, "create_subagent", insert), \
         patch.object(agents_api.database, "update_profile", update):
        result = await agents_api.create_profile_agent(
            "default",
            agents_api.ProfileSubagentRequest(
                name="New Agent", description="d", prompt="p"
            ),
            token=None,
        )

    assert result.id == "new-agent"
    assert update.threads, "the profile's enabled_agents was still updated"
    _assert_one_worker_hop(loop_thread, row, probe, insert, update)


# ═══ settings.py — the 21-query integrations endpoint ═══════════════════════


async def test_get_integration_settings_makes_all_reads_in_one_hop(
    mock_db, loop_thread
):
    setting = _ThreadSpy(settings_api.database.get_system_setting)
    key = _ThreadSpy(settings_api.get_decrypted_api_key)

    with patch.object(settings_api.database, "get_system_setting", setting), \
         patch.object(settings_api, "get_decrypted_api_key", key):
        response = await settings_api.get_integration_settings(token="t")

    # Still the whole payload, with the defaults applied on empty settings.
    assert response.tts_model == "fal-ai/kokoro/american-english"
    assert response.stt_model == "fal-ai/whisper"
    assert response.fal_api_key_set is False
    assert len(setting.threads) >= 19, "every setting is still read"
    assert len(key.threads) == 2
    _assert_one_worker_hop(loop_thread, setting, key)


async def test_get_integration_settings_does_not_stall_the_loop(mock_db):
    """The point of the offload: a slow settings read must not stop the world.

    Twenty-one reads at 5 ms each is >100 ms of frozen event loop; off-loop, a
    10 ms ticker keeps running throughout.
    """
    import time as _time

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_read(_key):
        _time.sleep(0.007)

    with patch.object(settings_api.database, "get_system_setting", slow_read), \
         patch.object(settings_api, "get_decrypted_api_key", lambda _n: None):
        task = asyncio.create_task(ticker())
        await settings_api.get_integration_settings(token="t")
        task.cancel()

    assert ticks >= 5, f"the event loop was stalled during the reads ({ticks} ticks)"


async def test_update_voice_settings_validates_and_writes_in_one_hop(
    mock_db, loop_thread
):
    """Validation, the writes and the closing re-read share one hop."""
    write = _ThreadSpy(settings_api.database.set_system_setting)
    read = _ThreadSpy(settings_api.database.get_system_setting)

    with patch.object(settings_api.database, "set_system_setting", write), \
         patch.object(settings_api.database, "get_system_setting", read):
        response = await settings_api.update_voice_settings(
            settings_api.VoiceSettingsUpdateRequest(voice_voice="quartz"),
            token=None,
        )

    assert response.voice_model == "gpt-live-1"
    assert response.voice_voice == "quartz"
    _assert_one_worker_hop(loop_thread, write, read)


@pytest.mark.parametrize("model", ["not-a-model", "gpt-realtime"])
async def test_update_voice_settings_rejects_a_bad_model_from_the_worker(mock_db, model):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await settings_api.update_voice_settings(
            settings_api.VoiceSettingsUpdateRequest(voice_model=model),
            token=None,
        )
    assert exc.value.status_code == 400


# ═══ settings_audio.py ══════════════════════════════════════════════════════


async def test_get_audio_models_reads_key_and_models_in_one_hop(mock_db, loop_thread):
    setting = _ThreadSpy(audio_api.database.get_system_setting)
    key = _ThreadSpy(audio_api.get_decrypted_api_key)

    with patch.object(audio_api.database, "get_system_setting", setting), \
         patch.object(audio_api, "get_decrypted_api_key", key):
        payload = await audio_api.get_audio_models(token="t")

    assert payload["current_tts"] == "fal-ai/kokoro/american-english"
    assert payload["current_stt"] == "fal-ai/whisper"
    assert payload["fal_configured"] is False
    _assert_one_worker_hop(loop_thread, setting, key)


async def test_update_tts_settings_writes_off_the_loop_in_one_hop(
    mock_db, loop_thread
):
    write = _ThreadSpy(audio_api.database.set_system_setting)

    with patch.object(audio_api.database, "set_system_setting", write):
        result = await audio_api.update_tts_settings(
            audio_api.TTSSettingsRequest(model="m", voice="v", speed=1.5),
            token=None,
        )

    assert result == {"success": True}
    assert len(write.threads) == 3
    _assert_one_worker_hop(loop_thread, write)


async def test_update_tts_settings_keeps_the_pre_validation_writes(mock_db):
    """A rejected speed still lands the model/voice writes that precede it —
    the original (odd) order, preserved by keeping the body in one closure."""
    from fastapi import HTTPException

    written = []
    with patch.object(
        audio_api.database, "set_system_setting",
        lambda k, v: written.append((k, v)),
    ), pytest.raises(HTTPException) as exc:
        await audio_api.update_tts_settings(
            audio_api.TTSSettingsRequest(model="m", voice="v", speed=99.0),
            token=None,
        )

    assert exc.value.status_code == 400
    assert written == [("tts_model", "m"), ("tts_voice", "v")]


# ═══ settings_credentials.py ════════════════════════════════════════════════


async def test_credential_policies_summary_probes_admin_keys_off_the_loop(
    mock_db, loop_thread
):
    """One listing plus one admin-key probe per policy — each probe is itself a
    settings read and a decrypt."""
    policies = [
        {"id": "fal_api_key", "policy": "optional", "description": "",
         "updated_at": "2026-08-28T00:00:00+00:00"},
    ]
    listing = _ThreadSpy(lambda: [dict(p) for p in policies])
    probe = _ThreadSpy(creds_api.get_decrypted_api_key)

    with patch.object(creds_api.database, "get_all_credential_policies", listing), \
         patch.object(creds_api, "get_decrypted_api_key", probe):
        payload = await creds_api.get_credential_policies_summary(token=None)

    assert payload["policies"][0]["effective_status"] == "optional_no_fallback"
    _assert_one_worker_hop(loop_thread, listing, probe)


async def test_set_user_credential_policy_keeps_check_and_write_atomic(
    mock_db, loop_thread
):
    """The user lookup gates the write; both must share one hop."""
    lookup = _ThreadSpy(lambda uid: {"id": uid, "name": "u"})
    write = _ThreadSpy(lambda *a: True)

    with patch.object(creds_api.database, "get_api_user", lookup), \
         patch.object(creds_api.database, "set_user_credential_policy", write):
        result = await creds_api.set_user_credential_policy(
            "user-1", "fal_api_key",
            creds_api.SetUserCredentialPolicyRequest(policy="user_provided"),
            token=None,
        )

    assert result["success"] is True
    _assert_one_worker_hop(loop_thread, lookup, write)


async def test_set_user_credential_policy_404s_for_an_unknown_user(mock_db):
    from fastapi import HTTPException

    with patch.object(creds_api.database, "get_api_user", lambda _uid: None), \
            pytest.raises(HTTPException) as exc:
        await creds_api.set_user_credential_policy(
            "nobody", "fal_api_key",
            creds_api.SetUserCredentialPolicyRequest(policy="user_provided"),
            token=None,
        )
    assert exc.value.status_code == 404


# ═══ settings_media.py ══════════════════════════════════════════════════════


async def test_update_video_settings_writes_in_one_hop(mock_db, loop_thread):
    write = _ThreadSpy(media_api.database.set_system_setting)

    with patch.object(media_api.database, "set_system_setting", write):
        result = await media_api.update_video_settings(
            media_api.VideoSettingsRequest(aspect_ratio="16:9", duration=5,
                                           resolution="1080p"),
            token=None,
        )

    assert result == {"success": True}
    assert len(write.threads) == 3
    _assert_one_worker_hop(loop_thread, write)


async def test_update_model_setting_writes_off_the_loop(mock_db, loop_thread):
    write = _ThreadSpy(media_api.database.set_system_setting)

    with patch.object(media_api.database, "set_system_setting", write):
        result = await media_api._update_model_setting(
            "image_model", media_api.ModelUpdateRequest(model="fal-ai/flux")
        )

    assert result == {"success": True, "model": "fal-ai/flux"}
    _assert_one_worker_hop(loop_thread, write)

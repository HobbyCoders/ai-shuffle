"""A turn on an API key's session runs as that key, whoever submitted it.

Regression tests for review finding R9-1 (ported from the rp9 probe): turns
submitted without ``payload.api_user_id`` ran as the owner, with the owner's
admin builtins (CronCreate, GetAppLogs, ...), the app-config MCP server and the
owner's credentials. The paths that did so: ScheduleWakeup / idle-watchdog
wakes, voice delegation, voice follow-ups to a worker and connector turns.

``SessionRuntime._resolve_principal`` is the single fallback: a turn without a
principal runs as the key that owns the session (walking worker parents). The
owner's own chat-socket message is the one way to run as the owner on a key's
session. Each submit path also passes the key explicitly, so every test below
checks both the submitted payload and the principal the runtime hands to the
engine, and the resulting tool surface and credential principal.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.api.auth import hash_api_key
from app.core import session_runtime, turn_manager
from app.db import database
from app.db import turns as turns_db

from tests.core.test_session_runtime import _runtime_env

KEY_SESSION = "key-sess"
OWNER_SESSION = "owner-sess"
PROFILE = "own-profile"


@pytest.fixture
def seeded(mock_db):
    database.create_profile(PROFILE, "Own", None, {"model": "sonnet"})
    database.create_api_user(
        "u1", "U1", hash_api_key("aih_principal_fallback"), profile_ids=[PROFILE],
    )
    database.create_session(KEY_SESSION, PROFILE, title="key", api_user_id="u1")
    database.create_session(OWNER_SESSION, PROFILE, title="owner")
    # A worker minted before workers recorded the key: NULL column, key parent.
    database.create_session(
        "legacy-worker", PROFILE, title="worker", is_worker=True,
        parent_session_id=KEY_SESSION,
    )
    return mock_db


async def _engine_principal(session_id, source, payload):
    """Run one turn through the real SessionRuntime; return the api_user_id the
    engine pump was started with."""
    turn = turns_db.enqueue_turn(
        session_id, source, prompt="go", profile_id=PROFILE, payload=payload,
    )
    seen = {}

    async def _pump(**kwargs):
        seen.update(kwargs)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_pump):
        await session_runtime.SessionRuntime(turn).execute()
    assert seen, "the turn never reached the engine"
    return seen["api_user_id"]


def _surface(session_id, api_user_id):
    """Build the real query options for a turn run as ``api_user_id``; return
    the builtins' admin flag, whether app-config was mounted, and the principal
    credentials were resolved for."""
    from app.core import query_options as qo
    from app.mcp import app_config_sdk, builtin_tools

    seen = {"app_config": False}
    real_env = qo._build_env_with_ai_tools

    def env_spy(*args, **kwargs):
        seen["credentials_for"] = args[2] if len(args) > 2 else kwargs.get("api_user_id")
        return real_env(*args, **kwargs)

    def builtins_spy(**kwargs):
        seen["builtins_is_admin"] = kwargs["is_admin"]
        return None

    def app_config_spy(**kwargs):
        seen["app_config"] = True
        return None

    with ExitStack() as stack:
        stack.enter_context(patch.object(qo, "_build_env_with_ai_tools", env_spy))
        stack.enter_context(patch.object(builtin_tools, "create_builtin_tools_server", builtins_spy))
        stack.enter_context(patch.object(app_config_sdk, "create_app_config_server", app_config_spy))
        qo.build_options_from_profile(
            database.get_profile(PROFILE), session_id=session_id, api_user_id=api_user_id,
        )
    return seen


def _assert_key_surface(session_id, principal):
    assert principal == "u1"
    surface = _surface(session_id, principal)
    assert surface["builtins_is_admin"] is False
    assert surface["app_config"] is False
    assert surface["credentials_for"] == "u1"


def _capture_submit(monkeypatch, result="turn-x"):
    captured = {}

    async def fake_submit(session_id, source, **kwargs):
        captured.update(kwargs, session_id=session_id, source=source)
        return result

    monkeypatch.setattr(turn_manager, "submit_async", fake_submit)
    return captured


# ---------------------------------------------------------------------------
# The runtime fallback
# ---------------------------------------------------------------------------


async def test_owner_surface_baseline_is_admin(seeded):
    """Guards the surface probe itself: the owner's options do mount the admin
    surface, so the key assertions below are not vacuous."""
    surface = _surface(OWNER_SESSION, None)
    assert surface["builtins_is_admin"] is True
    assert surface["app_config"] is True
    assert surface["credentials_for"] is None


@pytest.mark.parametrize("source", ["wakeup", "user", "queue", "connector", "auto_continue"])
async def test_a_turn_without_a_principal_runs_as_the_sessions_key(seeded, source):
    principal = await _engine_principal(KEY_SESSION, source, None)
    _assert_key_surface(KEY_SESSION, principal)


async def test_a_legacy_worker_runs_as_its_parents_key(seeded):
    principal = await _engine_principal("legacy-worker", "queue", None)
    _assert_key_surface("legacy-worker", principal)


async def test_owner_sessions_are_unchanged(seeded):
    for source in ("wakeup", "user", "connector"):
        assert await _engine_principal(OWNER_SESSION, source, None) is None
    assert await _engine_principal(
        OWNER_SESSION, "user", {"user_id": "admin"}) is None


@pytest.mark.parametrize("user_id", ["admin", "paired:phone-1"])
async def test_the_owners_own_socket_message_runs_as_the_owner(seeded, user_id):
    """The owner path: a message the owner typed or queued on their chat socket."""
    for source in ("user", "queue"):
        assert await _engine_principal(KEY_SESSION, source, {"user_id": user_id}) is None


@pytest.mark.parametrize("source", ["unpark", "auto_continue", "failover", "wakeup", "connector"])
async def test_an_owner_stamp_on_any_other_source_is_refused(seeded, source):
    """A continuation that copied the owner's socket stamp from an earlier turn
    is not the owner path: it runs as the session's key."""
    principal = await _engine_principal(KEY_SESSION, source, {"user_id": "admin"})
    _assert_key_surface(KEY_SESSION, principal)


async def test_an_explicit_key_principal_is_kept(seeded):
    assert await _engine_principal(
        OWNER_SESSION, "user", {"api_user_id": "u1", "user_id": "u1"}) == "u1"


async def test_an_unreadable_session_row_leaves_the_turn_queued(seeded):
    turn = turns_db.enqueue_turn(KEY_SESSION, "wakeup", prompt="go", profile_id=PROFILE)
    pump = AsyncMock()
    with _runtime_env(pump=pump), patch(
        "app.db.database.get_session", side_effect=RuntimeError("db down"),
    ):
        result = await session_runtime.SessionRuntime(turn).execute()
    assert result is None
    pump.assert_not_called()
    assert turns_db.get_turn(turn["id"])["state"] == "queued"


async def test_a_network_forwarded_turn_stays_correct(seeded, monkeypatch):
    """A turn a member forwards over the control channel keeps the key from the
    forwarding socket; one forwarded from the owner's socket runs as the owner."""
    from app.network import control

    monkeypatch.setattr(control, "_require_owned", lambda sid: database.get_session(sid))
    captured = _capture_submit(monkeypatch)

    await control._turns({
        "session_id": KEY_SESSION, "source": "user", "prompt": "hi",
        "payload": {"api_user_id": "u1", "user_id": "u1"},
    }, "member-device")
    assert captured["payload"]["forwarded_by_device"] == "member-device"
    principal = await _engine_principal(KEY_SESSION, "user", captured["payload"])
    _assert_key_surface(KEY_SESSION, principal)

    await control._turns({
        "session_id": OWNER_SESSION, "source": "user", "prompt": "hi",
        "payload": {"user_id": "admin"},
    }, "member-device")
    assert await _engine_principal(OWNER_SESSION, "user", captured["payload"]) is None


# ---------------------------------------------------------------------------
# Each submit path from the finding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wake_source", ["schedule_wakeup", "idle_watchdog"])
async def test_wakeup_runs_as_the_key_and_cannot_call_cron_create(seeded, monkeypatch, wake_source):
    from app.core import wakeup_service

    captured = _capture_submit(monkeypatch)
    monkeypatch.setattr(wakeup_service.wakeups_db, "claim_wakeup", lambda _id: True)
    monkeypatch.setattr(wakeup_service, "_session_user_stopped", lambda _sid: False)

    await wakeup_service._fire({
        "id": "w1", "session_id": KEY_SESSION, "profile_id": PROFILE,
        "prompt": "wake up", "source": wake_source,
    })

    assert captured["source"] == "wakeup"
    assert (captured.get("payload") or {}).get("api_user_id") == "u1"
    principal = await _engine_principal(KEY_SESSION, "wakeup", captured["payload"])
    _assert_key_surface(KEY_SESSION, principal)

    # The wake's builtins are the key's: CronCreate refuses.
    import app.mcp.inproc as sdk
    from app.mcp import builtin_tools

    found = {}
    real = sdk.create_tool_server

    def spy(name, version, tools):
        found.update({t.name: t for t in tools})
        return real(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=spy):
        builtin_tools.create_builtin_tools_server(
            session_id=KEY_SESSION, profile_id=PROFILE, is_admin=not principal, cwd="/tmp",
        )
    created = await found["CronCreate"].handler(
        {"name": "x", "expression": "* * * * *", "prompt": "p"})
    assert created.get("isError") is True


async def test_owner_session_wakeup_carries_no_key(seeded, monkeypatch):
    from app.core import wakeup_service

    captured = _capture_submit(monkeypatch)
    monkeypatch.setattr(wakeup_service.wakeups_db, "claim_wakeup", lambda _id: True)
    monkeypatch.setattr(wakeup_service, "_session_user_stopped", lambda _sid: False)
    await wakeup_service._fire({
        "id": "w2", "session_id": OWNER_SESSION, "profile_id": PROFILE,
        "prompt": "wake up", "source": "schedule_wakeup",
    })
    assert not (captured.get("payload") or {}).get("api_user_id")
    assert await _engine_principal(OWNER_SESSION, "wakeup", captured.get("payload")) is None


def _set_model(conn, session_id):
    conn.execute("UPDATE sessions SET model = ? WHERE id = ?", ("sonnet", session_id))
    conn.commit()


async def test_voice_delegation_runs_as_the_key(seeded, monkeypatch):
    from app.core import voice_stream

    _set_model(seeded, KEY_SESSION)
    captured = _capture_submit(monkeypatch)
    submitted = []

    async def on_submitted(turn_id):
        submitted.append(turn_id)

    await voice_stream.delegate_to_selected_model(KEY_SESSION, "do it", on_submitted=on_submitted)

    assert submitted == ["turn-x"] and captured["source"] == "user"
    assert captured["payload"]["user_message_already_recorded"] is True
    assert captured["payload"]["api_user_id"] == "u1"
    principal = await _engine_principal(KEY_SESSION, "user", captured["payload"])
    _assert_key_surface(KEY_SESSION, principal)


async def test_voice_delegation_on_an_owner_session_is_unchanged(seeded, monkeypatch):
    from app.core import voice_stream

    _set_model(seeded, OWNER_SESSION)
    captured = _capture_submit(monkeypatch)
    await voice_stream.delegate_to_selected_model(
        OWNER_SESSION, "do it", on_submitted=AsyncMock())
    assert captured["payload"] == {"user_message_already_recorded": True, "voice_delegation": True}


async def test_voice_follow_up_to_a_worker_runs_as_the_key(seeded, monkeypatch):
    from app.core import voice_bridge as vb
    from app.core.task_registry import task_registry

    turns_db.enqueue_turn("legacy-worker", "user", prompt="first", profile_id=PROFILE)
    monkeypatch.setattr(
        task_registry, "get_task",
        lambda sid, tid: {"id": tid, "kind": "agent", "status": "completed"},
    )
    monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock(return_value=0))
    captured = _capture_submit(monkeypatch)
    bridge = object.__new__(vb.VoiceBridge)
    bridge.session_id = KEY_SESSION
    bridge._start_watcher = lambda *a, **k: None
    bridge._ensure_approval_poller = lambda: None

    out = await bridge._tool_message_agent({"task_id": "legacy-worker", "message": "more"})

    assert out["ok"] is True and captured["source"] == "queue"
    assert captured["payload"] == {"api_user_id": "u1", "voice_delegation": True}
    principal = await _engine_principal("legacy-worker", "queue", captured["payload"])
    _assert_key_surface("legacy-worker", principal)


async def test_connector_turn_runs_as_the_connectors_key(seeded, monkeypatch):
    from app.core.connectors.base import IncomingMessage
    from app.core.connectors.manager import ConnectorManager
    from app.network import ownership

    monkeypatch.setattr(ownership, "enrolled", lambda: False)
    database.create_connector("c1", "discord", "Chat", {}, [], api_user_id="u1", is_active=True)
    captured = _capture_submit(monkeypatch)
    monkeypatch.setattr(turn_manager, "wait_session_idle", AsyncMock(return_value=False))

    await ConnectorManager().handle_incoming_message(IncomingMessage(
        connector_id="c1", platform_chat_id="chat-1", platform_user_id="p1",
        platform_user_name="Alice", text="hello", platform_message_id="m1",
    ))

    assert captured["source"] == "connector"
    # The connector's key is the principal; the sender (by platform id only)
    # rides along for the owner's-subscription check.
    assert captured["payload"] == {
        "api_user_id": "u1",
        "connector_sender": {"connector_id": "c1", "platform_user_id": "p1"},
    }
    ai_session = captured["session_id"]
    assert database.get_session(ai_session)["api_user_id"] == "u1"
    principal = await _engine_principal(ai_session, "connector", captured["payload"])
    _assert_key_surface(ai_session, principal)

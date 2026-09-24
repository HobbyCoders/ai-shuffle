"""The one credential gate (plan item C10).

Unit cases for ``agent_credentials_ready()`` and an integration test for a
fresh install: with no CLI sign-in and no stored key, every agent entry point
(a turn of any source through the real SessionRuntime and the real pump, and
the REST ``/query`` paths) is refused with the setup message, and no CLI
process is ever built.
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import claude_auth_mode, claude_cli_status, credential_gate, license as license_mod
from app.core import session_runtime
from app.db import database
from app.db import turns as turns_db

pytestmark = pytest.mark.real_agent_gate


def _cli(installed=True, signed_in=False, error=None):
    return claude_cli_status.ClaudeCliStatus(installed=installed, signed_in=signed_in, error=error)


# ---------------------------------------------------------------------------
# agent_credentials_ready()
# ---------------------------------------------------------------------------

def test_fresh_install_without_cli_is_blocked(mock_db):
    ready, reason = credential_gate.agent_credentials_ready()
    assert ready is False
    assert reason == credential_gate.NO_CLI_MESSAGE


def test_subscription_mode_needs_a_cli_sign_in(mock_db):
    claude_auth_mode.set_mode("subscription")
    assert credential_gate.agent_credentials_ready(_cli(signed_in=False)) == (
        False, credential_gate.NOT_SIGNED_IN_MESSAGE)
    assert credential_gate.agent_credentials_ready(_cli(signed_in=True)) == (True, None)


def test_subscription_mode_ignores_a_stored_key(mock_db):
    """Both present or only the key: the explicit mode decides, no fallback."""
    claude_auth_mode.set_mode("subscription")
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    ready, reason = credential_gate.agent_credentials_ready(_cli(signed_in=False))
    assert not ready and reason == credential_gate.NOT_SIGNED_IN_MESSAGE


def test_api_key_mode_needs_a_stored_key_not_a_cli_login(mock_db):
    claude_auth_mode.set_mode("api_key")
    assert credential_gate.agent_credentials_ready(_cli(signed_in=True)) == (
        False, credential_gate.NO_KEY_MESSAGE)
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    assert credential_gate.agent_credentials_ready(_cli(signed_in=False)) == (True, None)


def test_an_unlicensed_worker_is_blocked_with_the_license_reason(mock_db, monkeypatch):
    database.set_system_setting("anthropic_api_key", "sk-ant-api03-plain-ABCD")
    claude_auth_mode.set_mode("api_key")
    monkeypatch.setattr(license_mod, "license_allows_agent_runs", lambda: (False, "No license is active."))
    assert credential_gate.agent_credentials_ready() == (False, "No license is active.")
    # A provider-row turn needs no Claude credential, but the license still applies.
    assert credential_gate.turn_refusal("openai_responses") == "No license is active."


def test_provider_leg_turn_needs_no_claude_credential(mock_db):
    assert credential_gate.turn_refusal("openrouter") is None
    assert credential_gate.turn_refusal("delegated") == credential_gate.NO_CLI_MESSAGE


def test_startup_records_a_blocking_setup_state(mock_db):
    ready, reason = credential_gate.record_startup(_cli(installed=False, signed_in=None))
    assert not ready
    assert credential_gate.startup_state == "setup_required"
    assert credential_gate.status_payload(_cli(installed=False, signed_in=None))["reason"] == reason


def test_check_prerequisites_records_the_mode_and_the_gate(mock_db, monkeypatch):
    from app.core import startup

    monkeypatch.setattr(claude_cli_status, "get_status", lambda force=False, resolver=None: _cli(signed_in=False))
    startup.check_prerequisites()
    assert claude_auth_mode.stored_mode() == "subscription"
    assert credential_gate.startup_state == "setup_required"
    assert credential_gate.startup_reason == credential_gate.NOT_SIGNED_IN_MESSAGE


# ---------------------------------------------------------------------------
# /auth/status
# ---------------------------------------------------------------------------

async def test_auth_status_reports_the_gate(mock_db):
    from app.api import auth as auth_api

    request = MagicMock()
    request.cookies = {}
    request.headers = {}
    auth_api.reset_github_auth_cache()
    cli = {"installed": True, "signed_in": False, "email": None, "error": None}
    with patch.object(auth_api, "auth_service") as mock_auth, \
         patch.object(auth_api.owner_auth, "local_access", return_value=True), \
         patch.object(auth_api, "claude_cli_summary", AsyncMock(return_value=cli)):
        mock_auth.is_setup_required.return_value = False
        mock_auth.is_github_authenticated.return_value = False
        mock_auth.is_onboarding_complete.return_value = True
        status = await auth_api.get_auth_status(request)
    gate = status["agent_gate"]
    assert gate["ready"] is False
    assert gate["reason"] == credential_gate.NOT_SIGNED_IN_MESSAGE
    assert gate["mode"] == "subscription"
    assert status["has_any_credential"] is False


# ---------------------------------------------------------------------------
# Fresh install: every entry point refused
# ---------------------------------------------------------------------------

UNATTENDED_AND_INTERACTIVE_SOURCES = [
    "user", "queue", "api", "worktree", "connector", "automation", "wakeup",
    "provider_agent", "unpark", "auto_continue", "resume",
]


@contextmanager
def _real_pump_env():
    """SessionRuntime with the REAL pump; only the recorder, the task registry
    and the provider seam are stubbed. ``make_adapter`` fails the test if any
    code path tries to build a CLI process."""
    registry = MagicMock(name="task_registry")
    registry.is_user_stopped.return_value = False
    registry.stop_generation.return_value = 0
    registry.stop_invalidated.return_value = False
    registry.has_unresolved_background_launches.return_value = False
    registry.has_active_background_work.return_value = False
    registry.get_active_background_work.return_value = []
    registry.get_background_launches.return_value = set()

    recorder = MagicMock(name="recorder")
    recorder.start = AsyncMock()
    recorder.on_event = AsyncMock()
    recorder.close = AsyncMock()

    def _no_spawn(*_a, **_k):
        raise AssertionError("a refused turn must never build a CLI adapter")

    session_runtime.register_broadcaster(AsyncMock(name="broadcaster"))
    session_runtime.register_scheduler(MagicMock(name="on_turn_settled"))
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            session_runtime.TurnRecorder, "create_for_run", new=AsyncMock(return_value=recorder)))
        stack.enter_context(patch("app.core.session_runtime.record_parked", new=AsyncMock()))
        stack.enter_context(patch("app.core.session_runtime.start_idle_reader", new=MagicMock()))
        stack.enter_context(patch("app.core.session_runtime.stop_idle_reader", new=AsyncMock()))
        stack.enter_context(patch("app.core.task_registry.task_registry", new=registry))
        stack.enter_context(patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))
        stack.enter_context(patch("app.core.provider.factory.make_adapter", new=_no_spawn))
        yield recorder
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)


def _seed(tmp_path, sid):
    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path)})
    database.create_session(session_id=sid, profile_id="gate-profile", title="t")


@pytest.mark.parametrize("source", UNATTENDED_AND_INTERACTIVE_SOURCES)
async def test_fresh_install_refuses_a_turn_from_every_source(mock_db, tmp_path, source):
    sid = f"gate-{source}"
    _seed(tmp_path, sid)
    turn = turns_db.enqueue_turn(sid, source, prompt="hello", profile_id="gate-profile")
    turn_id = turn["id"]
    with _real_pump_env() as recorder:
        outcome = await session_runtime.execute(turn)
    assert outcome == "aborted"
    frames = [c.args[0] for c in recorder.on_event.await_args_list]
    assert [(f["type"], f["code"], f["message"]) for f in frames] == [
        ("error", credential_gate.SETUP_CODE, credential_gate.NO_CLI_MESSAGE)]
    assert turns_db.get_turn(turn_id)["state"] == "aborted"


async def test_fresh_install_refuses_the_rest_query_paths(mock_db, tmp_path):
    from app.api.query import require_claude_auth
    from app.core import query_engine
    from fastapi import HTTPException

    database.create_profile("gate-profile", "Gate", None, {"cwd": str(tmp_path)})
    with pytest.raises(HTTPException) as exc:
        await require_claude_auth()
    assert exc.value.status_code == 503 and exc.value.detail == credential_gate.NO_CLI_MESSAGE

    with patch("app.core.provider.factory.make_adapter", side_effect=AssertionError("no spawn")):
        frames = [f async for f in query_engine.stream_query("hello", "gate-profile")]
        assert frames[-1] == credential_gate.refusal_event(credential_gate.NO_CLI_MESSAGE)
        with pytest.raises(credential_gate.AgentSetupRequired, match="Agents cannot start"):
            await query_engine.execute_query("hello", "gate-profile")

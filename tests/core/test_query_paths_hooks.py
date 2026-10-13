"""
Regression tests for issue #567: hooks must be wired into EVERY SDK-session
spawn path, not just the WebSocket chat path.

Before the fix, build_hooks_config() was only called inside
stream_to_websocket, so DB hooks — including blocking PreToolUse security
guards — silently never fired on the HTTP paths:

- execute_query        → POST /api/v1/query, /conversation, automations,
                         connectors, ScheduleWakeup
- stream_query         → POST /api/v1/query/stream, /conversation/stream
- _run_background_query → POST /api/v1/conversation/start

All spawn paths now attach hooks via the shared
hook_service.attach_hooks_to_options() helper right after
build_options_from_profile(); these tests pin that wiring.
"""

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.hook_service as hook_service
import app.core.query_background as qb
import app.core.query_engine as qe

APP_SESSION_ID = "b19ddfa6-9357-4227-9c2f-a6b2f00c3b82"
SENTINEL_HOOKS = {"PreToolUse": ["sentinel-matcher"], "Stop": ["sentinel-stop"]}


def _make_options(cwd="/tmp/hooks-test-project"):
    """Options double: plain namespace so attribute writes are observable."""
    return types.SimpleNamespace(
        cwd=cwd,
        plugins=[{"path": "/plugins/ralph-loop"}, "not-a-dict"],
        hooks=None,
        stderr=None,
        model="claude-sonnet-5",
    )


async def _noop(*args, **kwargs):
    return None


# =============================================================================
# The shared helper itself
# =============================================================================

class TestAttachHooksToOptions:
    def test_attaches_hooks_and_forwards_args(self):
        options = _make_options()
        stderr_cb = object()
        with patch.object(
            hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
        ) as mock_build:
            hook_service.attach_hooks_to_options(
                options, project_id="proj-1", stderr_callback=stderr_cb
            )
        assert options.hooks == SENTINEL_HOOKS
        mock_build.assert_called_once_with(
            working_dir="/tmp/hooks-test-project",
            project_id="proj-1",
            plugin_paths=["/plugins/ralph-loop"],
            stderr_callback=stderr_cb,
            translated_main=False,
        )

    def test_translated_main_passed_when_env_marker_present(self):
        """The S8-R1 strip is opt-in: attach_hooks_to_options passes
        translated_main=True iff options.env carries AIH_GATEWAY_TRANSLATED_MAIN
        (presence, not truthiness)."""
        options = _make_options()
        options.env = {"AIH_GATEWAY_TRANSLATED_MAIN": "1", "PATH": "/usr/bin"}
        with patch.object(
            hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
        ) as mock_build:
            hook_service.attach_hooks_to_options(options)
        assert mock_build.call_args.kwargs["translated_main"] is True

    def test_translated_main_presence_not_truthiness(self):
        """An empty-string marker value still counts — detection keys on the key
        being present, not on its value being truthy."""
        options = _make_options()
        options.env = {"AIH_GATEWAY_TRANSLATED_MAIN": ""}
        with patch.object(
            hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
        ) as mock_build:
            hook_service.attach_hooks_to_options(options)
        assert mock_build.call_args.kwargs["translated_main"] is True

    def test_translated_main_false_without_marker(self):
        """No marker in options.env → translated_main=False."""
        options = _make_options()
        options.env = {"PATH": "/usr/bin"}
        with patch.object(
            hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
        ) as mock_build:
            hook_service.attach_hooks_to_options(options)
        assert mock_build.call_args.kwargs["translated_main"] is False

    def test_translated_main_false_when_no_env_attr(self):
        """Options without an env attribute at all → translated_main=False."""
        options = _make_options()
        # _make_options() namespace has no `env` attribute.
        assert not hasattr(options, "env")
        with patch.object(
            hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
        ) as mock_build:
            hook_service.attach_hooks_to_options(options)
        assert mock_build.call_args.kwargs["translated_main"] is False

    def test_noop_without_cwd(self):
        options = _make_options(cwd=None)
        with patch.object(hook_service, "build_hooks_config") as mock_build:
            hook_service.attach_hooks_to_options(options, project_id="proj-1")
        mock_build.assert_not_called()
        assert options.hooks is None

    def test_no_plugins_means_empty_plugin_paths(self):
        options = _make_options()
        options.plugins = None
        with patch.object(
            hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
        ) as mock_build:
            hook_service.attach_hooks_to_options(options)
        assert mock_build.call_args.kwargs["plugin_paths"] == []

    def test_empty_config_leaves_options_untouched(self):
        options = _make_options()
        with patch.object(hook_service, "build_hooks_config", return_value=None):
            hook_service.attach_hooks_to_options(options)
        assert options.hooks is None


# =============================================================================
# execute_query (POST /api/v1/query, /conversation, automations, connectors)
# =============================================================================

@pytest.mark.asyncio
async def test_execute_query_attaches_hooks():
    """Hooks must be on options BEFORE the adapter connect spawns the CLI."""
    options = _make_options()
    profile = {"id": "p1", "name": "PE"}
    session_row = {"id": APP_SESSION_ID, "sdk_session_id": None}

    # Spawn point is now adapter.connect_with_sdk_options(options) — the options
    # handed to it must already carry the hooks.
    adapter = MagicMock()
    adapter.connect_with_sdk_options = AsyncMock(
        side_effect=RuntimeError("stop at spawn")
    )
    adapter.send_query = AsyncMock()
    adapter.receive_events = MagicMock()
    adapter.disconnect = AsyncMock()

    with patch.object(qe, "get_profile", return_value=profile), patch.object(
        qe, "database"
    ) as mock_db, patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ), patch.object(
        qe, "resolve_note_references", side_effect=lambda p: p
    ), patch.object(
        qe, "reconcile_memory", _noop
    ), patch.object(
        qe, "push_memory", _noop
    ), patch.object(
        hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
    ) as mock_build, patch(
        "app.core.provider.factory.make_adapter", return_value=adapter
    ):
        mock_db.get_session.return_value = session_row
        with pytest.raises(RuntimeError, match="stop at spawn"):
            await qe.execute_query(
                prompt="do the thing",
                profile_id="p1",
                project_id="proj-http",
                session_id=APP_SESSION_ID,
            )

    assert options.hooks == SENTINEL_HOOKS, (
        "execute_query never attached hooks — DB PreToolUse guards do not "
        "fire on POST /api/v1/query, /conversation, automations, connectors"
    )
    assert mock_build.call_args.kwargs["project_id"] == "proj-http"


# =============================================================================
# stream_query (POST /api/v1/query/stream, /conversation/stream)
# =============================================================================

@pytest.mark.asyncio
async def test_stream_query_attaches_hooks():
    """Hooks must be attached by the time the init frame is yielded (the
    subsequent pool/connect machinery consumes options as-is)."""
    options = _make_options()
    profile = {"id": "p1", "name": "PE"}
    session_row = {"id": APP_SESSION_ID, "sdk_session_id": None}

    async def fake_run_sync(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch.object(qe, "get_profile", return_value=profile), patch.object(
        qe, "database"
    ) as mock_db, patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ), patch.object(
        qe, "run_sync", fake_run_sync
    ), patch.object(
        qe, "sync_engine"
    ) as mock_sync, patch.object(
        hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
    ) as mock_build:
        mock_db.get_session.return_value = session_row
        mock_db.add_session_message.return_value = {"id": 1, "role": "user"}
        mock_sync.broadcast_message_added = _noop

        gen = qe.stream_query(
            prompt="hi",
            profile_id="p1",
            project_id="proj-http",
            session_id=APP_SESSION_ID,
        )
        event = await gen.__anext__()
        await gen.aclose()

    assert event == {"type": "init", "session_id": APP_SESSION_ID}
    assert options.hooks == SENTINEL_HOOKS, (
        "stream_query never attached hooks — DB PreToolUse guards do not "
        "fire on POST /api/v1/query/stream and /conversation/stream"
    )
    assert mock_build.call_args.kwargs["project_id"] == "proj-http"


# =============================================================================
# _run_background_query (POST /api/v1/conversation/start)
# =============================================================================

@pytest.mark.asyncio
async def test_background_query_attaches_hooks():
    options = _make_options()
    profile = {"id": "p1", "name": "PE"}

    async def fake_run_sync(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    class SentinelStop(Exception):
        pass

    def poisoned_pool():
        # First pool access comes AFTER the option-build block — hooks must
        # already be attached when the session machinery starts.
        raise SentinelStop()

    with patch.object(qb, "database") as mock_db, patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})
    ), patch.object(
        qb, "run_sync", fake_run_sync
    ), patch.object(
        qb, "sync_engine"
    ) as mock_sync, patch.object(
        qb, "get_session_pool", poisoned_pool
    ), patch.object(
        hook_service, "build_hooks_config", return_value=SENTINEL_HOOKS
    ) as mock_build:
        mock_db.add_session_message.return_value = {"id": 1, "role": "user"}
        mock_sync.broadcast_message_added = _noop

        with pytest.raises(SentinelStop):
            await qb._run_background_query(
                session_id=APP_SESSION_ID,
                prompt="hi",
                profile=profile,
                project={"id": "proj-http"},
                overrides=None,
                resume_id=None,
                device_id=None,
                api_user_id=None,
            )

    assert options.hooks == SENTINEL_HOOKS, (
        "_run_background_query never attached hooks — DB PreToolUse guards "
        "do not fire on POST /api/v1/conversation/start"
    )
    assert mock_build.call_args.kwargs["project_id"] == "proj-http"

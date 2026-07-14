"""Non-WS intake callers submit durable turns through ``turn_manager`` (V2 A7).

Two server-originated spawn sites that used to call
``launch_headless_query`` now enqueue a durable turn via
``turn_manager.submit`` (the ONE spawn path via SessionRuntime):

* cloud intake — ``app.api.cloud._finalize_intake_session`` → ``source=intake``;
* worktree session — ``app.api.sessions_worktree.create_session_with_worktree``
  → ``source=worktree``.

``_finalize_intake_session`` is the shared tail of both cloud intake paths and
is small enough to drive directly; the worktree endpoint is large, so its
migration is pinned by a source guard alongside the intake one. Everything is
mocked at the handler/DB boundary — no app, no DB, no network.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock

import app.api.cloud as cloud_api
import app.core.turn_manager as turn_manager


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _wire(monkeypatch):
    monkeypatch.setattr(cloud_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(
        cloud_api.database, "get_session", lambda sid: {"id": sid, "title": ""}
    )
    monkeypatch.setattr(cloud_api.database, "update_session", lambda **kw: None)
    monkeypatch.setattr(cloud_api.database, "add_session_message", lambda **kw: None)

    import app.core.sync_engine as se
    monkeypatch.setattr(se.sync_engine, "broadcast_user_session_created", AsyncMock())

    submit = MagicMock(return_value="turn-i")
    monkeypatch.setattr(turn_manager, "submit", submit)
    return submit


async def test_finalize_intake_submits_intake_turn(monkeypatch):
    """The prompt/profile/project/worktree/overrides drive a durable
    ``source=intake`` turn; ``api_user_id`` rides the payload for attribution;
    the helper reports ``query_started=True``."""
    submit = _wire(monkeypatch)

    started = await cloud_api._finalize_intake_session(
        session={"id": "sess-1"},
        project_id="proj-A",
        prompt="do the intake",
        profile_id="default-ai",
        api_user_id="user-9",
        worktree_id="wt-1",
        overrides={"max_turns": 5},
    )

    assert started is True
    submit.assert_called_once()
    assert submit.call_args.args[0] == "sess-1"
    assert submit.call_args.args[1] == "intake"
    kw = submit.call_args.kwargs
    assert kw["prompt"] == "do the intake"
    assert kw["profile_id"] == "default-ai"
    assert kw["project_id"] == "proj-A"
    assert kw["worktree_id"] == "wt-1"
    assert kw["overrides"] == {"max_turns": 5}
    assert kw["payload"] == {"api_user_id": "user-9"}


async def test_finalize_intake_without_api_user_sends_no_payload(monkeypatch):
    submit = _wire(monkeypatch)

    await cloud_api._finalize_intake_session(
        session={"id": "sess-1"},
        project_id="proj-A",
        prompt="hi",
        profile_id="default-ai",
        api_user_id=None,
        worktree_id=None,
        overrides=None,
    )

    assert submit.call_args.kwargs["payload"] is None


async def test_finalize_intake_submit_failure_reports_not_started(monkeypatch):
    """A submit failure is swallowed so the endpoint still returns the created
    session (the FE falls back to a WS query) — query_started is False."""
    _wire(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(turn_manager, "submit", _boom)

    started = await cloud_api._finalize_intake_session(
        session={"id": "sess-1"},
        project_id="proj-A",
        prompt="hi",
        profile_id="default-ai",
        api_user_id=None,
        worktree_id=None,
        overrides=None,
    )

    assert started is False


def test_intake_and_worktree_callers_use_turn_manager_not_headless():
    """Both non-WS spawn sites route through ``turn_manager.submit`` with their
    source string and no longer reference the deleted headless runner."""
    import app.api.sessions_worktree as worktree_api

    intake_src = inspect.getsource(cloud_api._finalize_intake_session)
    assert "turn_manager.submit" in intake_src
    assert '"intake"' in intake_src
    assert "launch_headless_query" not in intake_src

    worktree_src = inspect.getsource(worktree_api.create_session_with_worktree)
    assert "turn_manager.submit" in worktree_src
    assert '"worktree"' in worktree_src
    assert "launch_headless_query" not in worktree_src

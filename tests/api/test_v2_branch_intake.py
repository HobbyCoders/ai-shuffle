"""V2 Phase 3 branch-intake cutover (A4).

A NEW branch session records ONLY its logical ``sessions.branch`` +
origin-keyed ``project_v2_id``. It does NOT create a legacy ``worktrees`` row
or md5-hash worktree dir; ``worktree_id``/``worktree_branch``/``worktree_path``
stay NULL. The deterministic per-runner checkout
(``{workspace}/checkouts/{project}/{branch}``) is materialized via
``checkout_manager.ensure_checkout`` and re-materialized idempotently by the
origin resolver at spawn.

Legacy worktree sessions (existing rows, flag off) keep working through the
unchanged ``worktree_manager.create_worktree_session`` path.

Everything is mocked at the handler/DB boundary — no app, no DB, no git, no
network.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.sessions_worktree as worktree_api
from app.api.sessions_helpers import CreateWorktreeSessionRequest
from fastapi import HTTPException


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _body(**over):
    base = dict(
        project_id="proj-A",
        branch_name="feature/x",
        base_branch="main",
        profile_id=None,
        create_new_branch=True,
    )
    base.update(over)
    return CreateWorktreeSessionRequest(**base)


def _wire_v2(monkeypatch, *, pv2=None, checkout_path="/ws/checkouts/proj-A/feature-x"):
    """Wire the v2 branch-intake helper's collaborators. Returns a dict of the
    key MagicMocks the tests assert on."""
    pv2 = pv2 or {"id": "pv2-abc123", "origin": "git:example.com/repo"}

    monkeypatch.setattr(worktree_api, "run_sync", _passthrough_run_sync)

    import app.core.project_resolver as project_resolver
    import app.core.checkout_manager as checkout_manager

    resolve_pv2 = MagicMock(return_value=pv2)
    ensure_checkout = MagicMock(return_value=checkout_path)
    monkeypatch.setattr(project_resolver, "resolve_project_v2", resolve_pv2)
    monkeypatch.setattr(checkout_manager, "ensure_checkout", ensure_checkout)

    create_session = MagicMock()
    set_session_branch = MagicMock()
    monkeypatch.setattr(worktree_api.database, "create_session", create_session)
    monkeypatch.setattr(worktree_api.database, "set_session_branch", set_session_branch)
    monkeypatch.setattr(
        worktree_api.database, "get_all_profiles", lambda: [{"id": "default-ai"}]
    )
    monkeypatch.setattr(
        worktree_api.database, "get_session", lambda sid: {"id": sid, "title": ""}
    )
    monkeypatch.setattr(worktree_api.database, "update_session", lambda **kw: None)
    monkeypatch.setattr(worktree_api.database, "add_session_message", lambda **kw: None)

    import app.core.sync_engine as se
    monkeypatch.setattr(se.sync_engine, "broadcast_user_session_created", AsyncMock())
    monkeypatch.setattr(se.sync_engine, "broadcast_message_added", AsyncMock())
    monkeypatch.setattr(se.sync_engine, "broadcast_session_updated", AsyncMock())

    import app.core.turn_manager as turn_manager
    submit = MagicMock(return_value="turn-w")
    monkeypatch.setattr(turn_manager, "submit", submit)

    # A worktree row must NEVER be created on the v2 path.
    import app.core.worktree_manager as wm
    create_wt = MagicMock(side_effect=AssertionError("legacy worktree row created!"))
    monkeypatch.setattr(wm.worktree_manager, "create_worktree_session", create_wt)

    return {
        "pv2": pv2,
        "resolve_pv2": resolve_pv2,
        "ensure_checkout": ensure_checkout,
        "create_session": create_session,
        "set_session_branch": set_session_branch,
        "submit": submit,
        "create_worktree_session": create_wt,
    }


async def test_create_with_branch_persists_branch_and_no_worktree_row(monkeypatch):
    """Create (no prompt) persists ``sessions.branch`` + project_v2_id via
    ``set_session_branch`` and never touches the legacy worktree machinery. The
    response carries a NULL ``worktree_id`` and the materialized checkout path."""
    m = _wire_v2(monkeypatch)

    resp = await worktree_api._create_v2_branch_session(
        request=MagicMock(),
        body=_body(),
        project={"id": "proj-A", "settings": {}},
        api_user_id="user-9",
    )

    # Origin project resolved, checkout materialized for (pv2, branch).
    m["resolve_pv2"].assert_called_once()
    m["ensure_checkout"].assert_called_once()
    assert m["ensure_checkout"].call_args.args[0] == m["pv2"]
    assert m["ensure_checkout"].call_args.args[1] == "feature/x"

    # Session created WITHOUT a worktree_id, then branch+pv2 stamped.
    m["create_session"].assert_called_once()
    assert "worktree_id" not in m["create_session"].call_args.kwargs
    m["set_session_branch"].assert_called_once()
    sb_args = m["set_session_branch"].call_args.args
    assert sb_args[1] == "feature/x"
    assert sb_args[2] == "pv2-abc123"

    # No legacy worktrees row was created (mock would have raised).
    m["create_worktree_session"].assert_not_called()

    # Response reports branch mode with a NULL worktree_id.
    assert resp.worktree_id is None
    assert resp.branch_name == "feature/x"
    assert resp.worktree_path == "/ws/checkouts/proj-A/feature-x"
    assert resp.query_started is False


async def test_promptless_compose_migrates_pending_uploads(monkeypatch):
    """A promptless compose (pending_upload_paths, NO prompt) still migrates the
    uploads into the materialized checkout — the migration is hoisted out of the
    prompt block, so attachments land even when no turn is submitted."""
    m = _wire_v2(monkeypatch)
    migrate = MagicMock(return_value=[])
    monkeypatch.setattr(worktree_api, "_migrate_uploads_to_dir", migrate)

    resp = await worktree_api._create_v2_branch_session(
        request=MagicMock(),
        body=_body(prompt=None, pending_upload_paths=["/ws/proj-A/a.png"]),
        project={"id": "proj-A", "settings": {}},
        api_user_id=None,
    )

    # Migration ran against the materialized checkout path with the pending files.
    migrate.assert_called_once()
    assert migrate.call_args.args[0] == "proj-A"
    assert migrate.call_args.args[1] == Path("/ws/checkouts/proj-A/feature-x")
    assert migrate.call_args.args[2] == ["/ws/proj-A/a.png"]

    # No prompt → no turn submitted, but the session (and its uploads) still exist.
    m["submit"].assert_not_called()
    assert resp.query_started is False
    assert resp.upload_migration_errors is None


async def test_spawn_materializes_deterministic_checkout_and_submits_worktree_turn(
    monkeypatch,
):
    """With a prompt, the checkout is materialized (stubbed ensure_checkout) and a
    durable ``source=worktree`` turn is submitted with NO ``worktree_id`` — the
    spawn path re-resolves cwd from the origin resolver off branch+project_v2."""
    m = _wire_v2(monkeypatch)

    resp = await worktree_api._create_v2_branch_session(
        request=MagicMock(),
        body=_body(prompt="build the feature"),
        project={"id": "proj-A", "settings": {}},
        api_user_id=None,
    )

    m["ensure_checkout"].assert_called_once()
    assert m["ensure_checkout"].call_args.args[1] == "feature/x"

    m["submit"].assert_called_once()
    assert m["submit"].call_args.args[0] == resp.session_id
    assert m["submit"].call_args.args[1] == "worktree"
    kw = m["submit"].call_args.kwargs
    assert kw["prompt"] == "build the feature"
    assert kw["project_id"] == "proj-A"
    assert "worktree_id" not in kw
    assert resp.query_started is True


async def test_default_profile_falls_back_to_project_then_first(monkeypatch):
    """No explicit profile → project default → first profile."""
    m = _wire_v2(monkeypatch)

    await worktree_api._create_v2_branch_session(
        request=MagicMock(),
        body=_body(profile_id=None),
        project={"id": "proj-A", "settings": {"default_profile_id": "proj-default"}},
        api_user_id=None,
    )
    assert m["create_session"].call_args.kwargs["profile_id"] == "proj-default"


async def test_unresolvable_project_raises_500(monkeypatch):
    m = _wire_v2(monkeypatch)
    m["resolve_pv2"].return_value = None

    with pytest.raises(HTTPException) as ei:
        await worktree_api._create_v2_branch_session(
            request=MagicMock(),
            body=_body(),
            project={"id": "proj-A", "settings": {}},
            api_user_id=None,
        )
    assert ei.value.status_code == 500
    m["ensure_checkout"].assert_not_called()


async def test_missing_base_checkout_raises_409(monkeypatch):
    """ensure_checkout returns None (project not checked out on this runner —
    Phase 3 never clones) → honest 409, no session created."""
    m = _wire_v2(monkeypatch)
    m["ensure_checkout"].return_value = None

    with pytest.raises(HTTPException) as ei:
        await worktree_api._create_v2_branch_session(
            request=MagicMock(),
            body=_body(),
            project={"id": "proj-A", "settings": {}},
            api_user_id=None,
        )
    assert ei.value.status_code == 409
    m["create_session"].assert_not_called()
    m["set_session_branch"].assert_not_called()


# ---------------------------------------------------------------------------
# R5.3: intake threads base_branch/create_new_branch into ensure_checkout, and
# the response reports the ACTUAL base honestly.
# ---------------------------------------------------------------------------


async def test_ensure_checkout_receives_base_and_create_new_kwargs(monkeypatch):
    m = _wire_v2(monkeypatch)
    await worktree_api._create_v2_branch_session(
        request=MagicMock(),
        body=_body(base_branch="develop", create_new_branch=True),
        project={"id": "proj-A", "settings": {}},
        api_user_id=None,
    )
    kw = m["ensure_checkout"].call_args.kwargs
    assert kw["base_branch"] == "develop"
    assert kw["create_new_branch"] is True


async def test_response_base_branch_honest_for_new_branch(monkeypatch):
    _wire_v2(monkeypatch)
    resp = await worktree_api._create_v2_branch_session(
        request=MagicMock(),
        body=_body(base_branch="develop", create_new_branch=True),
        project={"id": "proj-A", "settings": {}},
        api_user_id=None,
    )
    # A newly-minted branch is honestly forked from base_branch.
    assert resp.base_branch == "develop"


async def test_response_base_branch_honest_for_existing_branch(monkeypatch):
    _wire_v2(monkeypatch)
    resp = await worktree_api._create_v2_branch_session(
        request=MagicMock(),
        body=_body(base_branch="develop", create_new_branch=False),
        project={"id": "proj-A", "settings": {}},
        api_user_id=None,
    )
    # An existing / origin-recovered branch materialized from ITSELF — the
    # response echoes its own name, never a false fork point.
    assert resp.base_branch == "feature/x"


async def test_missing_branch_non_new_raises_409_with_branch_message(monkeypatch):
    """create_new_branch=False + not materializable → 409 pointing at the branch
    (push it / create as new), distinct from the base-not-checked-out message."""
    m = _wire_v2(monkeypatch)
    m["ensure_checkout"].return_value = None

    with pytest.raises(HTTPException) as ei:
        await worktree_api._create_v2_branch_session(
            request=MagicMock(),
            body=_body(create_new_branch=False),
            project={"id": "proj-A", "settings": {}},
            api_user_id=None,
        )
    assert ei.value.status_code == 409
    assert "origin" in ei.value.detail.lower()
    m["create_session"].assert_not_called()


# ---------------------------------------------------------------------------
# Routing: the /with-worktree endpoint ALWAYS delegates to v2 branch-intake
# (PR-B removed the ``v2_projects`` kill switch — no legacy worktree fork).
# ---------------------------------------------------------------------------


async def test_endpoint_delegates_to_v2_branch_intake(monkeypatch):
    """``create_session_with_worktree`` unconditionally routes to
    ``_create_v2_branch_session`` (after the project-exists / access checks);
    the legacy ``worktree_manager.create_worktree_session`` is never called."""
    import app.core.worktree_manager as wm

    monkeypatch.setattr(worktree_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(
        worktree_api.database, "get_project", lambda pid: {"id": pid, "settings": {}}
    )
    monkeypatch.setattr(worktree_api, "get_api_user_from_request", lambda req: None)

    v2_helper = AsyncMock(return_value="v2-response")
    monkeypatch.setattr(worktree_api, "_create_v2_branch_session", v2_helper)

    legacy_create = MagicMock(
        side_effect=AssertionError("legacy worktree path taken!")
    )
    monkeypatch.setattr(wm.worktree_manager, "create_worktree_session", legacy_create)

    resp = await worktree_api.create_session_with_worktree(
        request=MagicMock(),
        body=_body(prompt=""),
        token="tok",
    )

    v2_helper.assert_awaited_once()
    legacy_create.assert_not_called()
    assert resp == "v2-response"

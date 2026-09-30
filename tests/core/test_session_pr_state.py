# persist_pr_state + turn-end reconcile.

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.session_pr_state import persist_pr_state, reconcile_session_pr
from app.db import database


def _mk(**kwargs):
    sid = str(uuid.uuid4())
    if kwargs.get('project_id'):
        try:
            database.create_project(kwargs['project_id'], 'P', None, '/tmp/p')
        except Exception:
            pass
    database.create_session(session_id=sid, profile_id='default', **kwargs)
    return sid


@pytest.mark.asyncio
async def test_persist_pr_state_writes_and_broadcasts(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.set_session_active(sid, True)

    with patch('app.core.sync_engine.sync_engine') as sync:
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        await persist_pr_state(
            session_id=sid,
            project_id='p1',
            branch='feat/x',
            pr_number=9,
            pr_url='https://github.com/o/r/pull/9',
            pr_state='open',
        )

    row = database.get_session(sid)
    assert row['pr_number'] == 9
    assert row['pr_state'] == 'open'
    assert row['is_active']
    sync.broadcast_user_session_updated.assert_awaited()
    payload = sync.broadcast_user_session_updated.await_args.kwargs
    assert payload['session_id'] == sid
    assert payload['updates']['pr_state'] == 'open'
    assert payload['updates']['pr_number'] == 9


@pytest.mark.asyncio
async def test_persist_merged_does_not_settle(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.set_session_active(sid, True)
    with patch('app.core.sync_engine.sync_engine') as sync:
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        await persist_pr_state(
            session_id=sid,
            pr_number=9,
            pr_url='https://github.com/o/r/pull/9',
            pr_state='merged',
        )
    row = database.get_session(sid)
    assert row['pr_state'] == 'merged'
    assert row['is_active']


@pytest.mark.asyncio
async def test_reconcile_persists_open_pr_for_branch(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    pr = SimpleNamespace(
        number=4, url='https://github.com/o/r/pull/4', state='OPEN', head_branch='feat/x'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value='/wd')
    ), patch('app.core.github_service.github_service') as gh, patch(
        'app.core.sync_engine.sync_engine'
    ) as sync:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = [pr]
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        await reconcile_session_pr(sid)

    row = database.get_session(sid)
    assert row['pr_number'] == 4
    assert row['pr_state'] == 'open'
    assert row['pr_url'] == 'https://github.com/o/r/pull/4'


@pytest.mark.asyncio
async def test_reconcile_merged_adopts_new_open_pr(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='merged'
    )
    new_pr = SimpleNamespace(
        number=12, url='https://github.com/o/r/pull/12', state='OPEN', head_branch='feat/x'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value='/wd')
    ), patch('app.core.github_service.github_service') as gh, patch(
        'app.core.sync_engine.sync_engine'
    ) as sync:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = [new_pr]
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        await reconcile_session_pr(sid)
        gh.get_pull.assert_not_called()

    row = database.get_session(sid)
    assert row['pr_number'] == 12
    assert row['pr_state'] == 'open'
    assert row['pr_url'] == 'https://github.com/o/r/pull/12'


@pytest.mark.asyncio
async def test_reconcile_merged_without_new_pr_keeps_state(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='merged'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value='/wd')
    ), patch('app.core.github_service.github_service') as gh:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = []
        await reconcile_session_pr(sid)
        gh.get_pull.assert_not_called()

    row = database.get_session(sid)
    assert row['pr_number'] == 4
    assert row['pr_state'] == 'merged'
    assert row['pr_url'] == 'https://github.com/o/r/pull/4'


@pytest.mark.asyncio
async def test_reconcile_same_merged_pr_does_not_rewrite(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='merged'
    )
    same = SimpleNamespace(
        number=4, url='https://github.com/o/r/pull/4', state='MERGED', head_branch='feat/x'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value='/wd')
    ), patch('app.core.github_service.github_service') as gh, patch(
        'app.core.sync_engine.sync_engine'
    ) as sync:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = [same]
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        await reconcile_session_pr(sid)
        sync.broadcast_user_session_updated.assert_not_awaited()

    row = database.get_session(sid)
    assert row['pr_number'] == 4
    assert row['pr_state'] == 'merged'


@pytest.mark.asyncio
async def test_reconcile_merged_adopts_newer_merged_pr(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='merged'
    )
    old_pr = SimpleNamespace(
        number=4, url='https://github.com/o/r/pull/4', state='MERGED', head_branch='feat/x'
    )
    new_pr = SimpleNamespace(
        number=12, url='https://github.com/o/r/pull/12', state='MERGED', head_branch='feat/x'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value='/wd')
    ), patch('app.core.github_service.github_service') as gh, patch(
        'app.core.sync_engine.sync_engine'
    ) as sync:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = [old_pr, new_pr]
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        await reconcile_session_pr(sid)
        gh.get_pull.assert_not_called()
        gh.list_pulls.assert_called()
        assert gh.list_pulls.call_args.kwargs.get('state') == 'all'
        assert gh.list_pulls.call_args.kwargs.get('head') == 'feat/x'

    row = database.get_session(sid)
    assert row['pr_number'] == 12
    assert row['pr_state'] == 'merged'
    assert row['pr_url'] == 'https://github.com/o/r/pull/12'


@pytest.mark.asyncio
async def test_reconcile_prefers_open_pr_over_older_merged(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='merged'
    )
    old_pr = SimpleNamespace(
        number=4, url='https://github.com/o/r/pull/4', state='MERGED', head_branch='feat/x'
    )
    new_pr = SimpleNamespace(
        number=12, url='https://github.com/o/r/pull/12', state='OPEN', head_branch='feat/x'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value='/wd')
    ), patch('app.core.github_service.github_service') as gh, patch(
        'app.core.sync_engine.sync_engine'
    ) as sync:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = [old_pr, new_pr]
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        await reconcile_session_pr(sid)

    row = database.get_session(sid)
    assert row['pr_number'] == 12
    assert row['pr_state'] == 'open'


@pytest.mark.asyncio
async def test_reconcile_failed_lookup_does_not_clear(mock_db):
    from app.core.github_service import GitHubCliError

    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://x/4', pr_state='open'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value='/wd')
    ), patch('app.core.github_service.github_service') as gh:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.side_effect = GitHubCliError('offline')
        await reconcile_session_pr(sid)

    row = database.get_session(sid)
    assert row['pr_state'] == 'open'
    assert row['pr_number'] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
async def test_reconcile_canonical_session_without_legacy_project(mock_db, tmp_path, state):
    """PR discovery must use the owner checkout even without a legacy alias."""
    from app.core.config import settings
    from app.db import checkouts, projects_v2

    project = projects_v2.upsert_project("git:canonical-pr", name="Repo")
    sid = _mk(project_v2_id=project["id"])
    database.set_session_branch(sid, "feat/canonical")
    checkouts.upsert_checkout(
        project["id"], settings.effective_instance_id, str(tmp_path), branch="feat/canonical"
    )
    pr = SimpleNamespace(
        number=1522, url="https://github.com/o/r/pull/1522",
        state=state, head_branch="feat/canonical",
    )
    # Use the real working-directory resolver and the real persistence path.
    with patch("app.core.github_service.github_service") as gh, patch(
        "app.core.sync_engine.sync_engine"
    ) as sync:
        gh.get_repo_from_remote.return_value = "o/r"
        gh.list_pulls.return_value = [pr]
        sync.broadcast_session_updated = AsyncMock()
        sync.broadcast_user_session_updated = AsyncMock()
        assert await reconcile_session_pr(sid)
        gh.get_repo_from_remote.assert_called_once_with(str(tmp_path))
        gh.list_pulls.assert_called_once_with("o/r", state="all", limit=100, head="feat/canonical")

    row = database.get_session(sid)
    assert row["project_id"] is None
    assert row["pr_number"] == 1522
    assert row["pr_url"] == pr.url
    assert row["pr_state"] == state.lower()
    payload = sync.broadcast_user_session_updated.await_args.kwargs
    assert payload["session_id"] == sid
    assert payload["updates"]["pr_number"] == 1522
    assert payload["updates"]["pr_state"] == state.lower()


@pytest.mark.asyncio
async def test_reconcile_canonical_session_missing_checkout_does_not_use_base(mock_db):
    from app.db import projects_v2

    project = projects_v2.upsert_project("git:missing-pr-checkout", name="Repo")
    sid = _mk(project_v2_id=project["id"])
    database.set_session_branch(sid, "feat/missing")
    with patch("app.core.github_service.github_service") as gh:
        assert not await reconcile_session_pr(sid)
        gh.get_repo_from_remote.assert_not_called()
    assert database.get_session(sid)["pr_state"] is None

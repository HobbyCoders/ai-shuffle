# persist_pr_state + turn-end reconcile.

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

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
    gh.list_pulls.assert_called_once_with('o/r', state='all', limit=100, head='feat/x')
    gh.list_commit_pulls.assert_not_called()


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
async def test_reconcile_merged_without_new_pr_keeps_state(mock_db, pr_checkout):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='merged'
    )

    with patch(
        'app.api.git_helpers.get_working_dir_async', new=AsyncMock(return_value=str(pr_checkout))
    ), patch('app.core.github_service.github_service') as gh:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = []
        gh.list_commit_pulls.return_value = []
        assert await reconcile_session_pr(sid)
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
    from app.db.async_database import run_sync
    from tests.git_repo_helpers import init_repo

    await run_sync(init_repo, tmp_path, branch="feat/canonical")
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


def _listed(sid):
    return [
        (e['number'], e['state'], e['source'])
        for e in database.decode_pull_requests(database.get_session(sid)['pull_requests'])
    ]


def _quiet_sync():
    sync = patch('app.core.sync_engine.sync_engine')
    mock = sync.start()
    mock.broadcast_session_updated = AsyncMock()
    mock.broadcast_user_session_updated = AsyncMock()
    return sync, mock


@pytest.mark.asyncio
async def test_subagent_pr_is_listed_on_its_parent_chat(mock_db):
    parent = _mk(project_id='p1', worktree_branch='feat/parent')
    database.update_session_pr_state(
        parent, pr_number=3, pr_url='https://github.com/o/r/pull/3', pr_state='open'
    )
    middle = _mk(project_id='p1', parent_session_id=parent, is_worker=True, hidden=True,
                 worktree_branch='task/m')
    worker = _mk(project_id='p1', parent_session_id=middle, is_worker=True, hidden=True,
                 worktree_branch='task/w')
    sync, mock = _quiet_sync()
    try:
        await persist_pr_state(
            session_id=worker, project_id='p1', branch='task/w', pr_number=21,
            pr_url='https://github.com/o/r/pull/21', pr_state='open', pr_title='Worker fix',
        )
        # The parent keeps its own PR as the primary and lists both.
        row = database.get_session(parent)
        assert (row['pr_number'], row['pr_state']) == (3, 'open')
        assert _listed(parent) == [(3, 'open', 'session'), (21, 'open', 'subagent')]
        assert _listed(middle) == [(21, 'open', 'subagent')]
        assert database.get_session(middle)['pr_number'] is None
        parent_patch = [
            c.kwargs for c in mock.broadcast_user_session_updated.await_args_list
            if c.kwargs['session_id'] == parent
        ][-1]['updates']
        assert 'pr_number' not in parent_patch
        assert [e['number'] for e in parent_patch['pull_requests']] == [3, 21]

        # Merging the worker's PR (by number, as a merge webhook does) reaches the parent.
        await persist_pr_state(pr_number=21, pr_url='https://github.com/o/r/pull/21',
                               pr_state='merged')
        assert _listed(parent) == [(3, 'open', 'session'), (21, 'merged', 'subagent')]
        assert database.get_session(parent)['pr_state'] == 'open'
    finally:
        sync.stop()


@pytest.mark.asyncio
async def test_fork_pr_is_not_listed_on_its_source_chat(mock_db):
    source = _mk(project_id='p1', worktree_branch='feat/a')
    fork = _mk(project_id='p1', parent_session_id=source, worktree_branch='feat/b')
    sync, _ = _quiet_sync()
    try:
        await persist_pr_state(session_id=fork, pr_number=8,
                               pr_url='https://github.com/o/r/pull/8', pr_state='open')
    finally:
        sync.stop()
    assert _listed(fork) == [(8, 'open', 'session')]
    assert _listed(source) == []


@pytest.mark.asyncio
async def test_reconcile_new_branch_keeps_older_pr_and_refreshes_it(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/two')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='open',
        pr_branch='feat/one',
    )
    new_pr = SimpleNamespace(number=9, url='https://github.com/o/r/pull/9', state='OPEN',
                             head_branch='feat/two', title='Second')
    old_now = SimpleNamespace(number=4, url='https://github.com/o/r/pull/4', state='MERGED',
                              head_branch='feat/one', title='First')
    sync, _ = _quiet_sync()
    try:
        with patch('app.api.git_helpers.get_working_dir_async',
                   new=AsyncMock(return_value='/wd')), \
                patch('app.core.github_service.github_service') as gh:
            gh.get_repo_from_remote.return_value = 'o/r'
            gh.list_pulls.return_value = [new_pr]
            gh.get_pull.return_value = old_now
            assert await reconcile_session_pr(sid)
            gh.get_pull.assert_called_once_with('o/r', 4)
    finally:
        sync.stop()
    row = database.get_session(sid)
    assert (row['pr_number'], row['pr_state']) == (9, 'open')
    entries = database.decode_pull_requests(row['pull_requests'])
    assert [(e['number'], e['state'], e['title']) for e in entries] == [
        (4, 'merged', 'First'), (9, 'open', 'Second'),
    ]


@pytest.mark.asyncio
async def test_reconcile_skips_listed_prs_from_another_repo(mock_db):
    sid = _mk(project_id='p1', worktree_branch='feat/x')
    database.update_session_pr_state(
        sid, pr_number=4, pr_url='https://github.com/o/r/pull/4', pr_state='open'
    )
    database.update_session_pr_state(
        sid, pr_number=5, pr_url='https://github.com/other/repo/pull/5', pr_state='open',
        primary=False,
    )
    same = SimpleNamespace(number=4, url='https://github.com/o/r/pull/4', state='OPEN',
                           head_branch='feat/x', title='T')
    with patch('app.api.git_helpers.get_working_dir_async',
               new=AsyncMock(return_value='/wd')), \
            patch('app.core.github_service.github_service') as gh:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = [same]
        assert await reconcile_session_pr(sid)
        gh.get_pull.assert_not_called()


@pytest.fixture
def pr_checkout(tmp_path, monkeypatch):
    """A fresh linked worktree with a local bare origin, never the host repo."""
    from tests.git_repo_helpers import git, hermetic_git_env, init_repo, make_origin

    hermetic_git_env(monkeypatch, tmp_path)
    repo = init_repo(tmp_path / 'repo')
    make_origin(tmp_path, repo)
    checkout = tmp_path / 'checkout'
    git(repo, 'worktree', 'add', '-q', '-b', 'feat/local', str(checkout), 'origin/main')
    return checkout


def _checkout_pull(number, state, head_sha, head='feat/remote'):
    return SimpleNamespace(
        number=number, state=state, head_branch=head, head_sha=head_sha,
        base_branch='main', head_repo='o/r', title='Renamed remote branch',
        url=f'https://github.com/o/r/pull/{number}',
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['OPEN', 'MERGED'])
@pytest.mark.parametrize('local_ahead', [False, True])
async def test_reconcile_finds_differently_named_upstream(mock_db, pr_checkout, state, local_ahead):
    from tests.git_repo_helpers import commit_file, git

    sha = commit_file(pr_checkout, 'feature.txt', 'feature', 'feature')
    git(pr_checkout, 'push', '-q', '-u', 'origin', 'HEAD:feat/remote')
    assert git(pr_checkout, 'rev-parse', '--abbrev-ref', '@{u}') == 'origin/feat/remote'
    if local_ahead:
        commit_file(pr_checkout, 'later.txt', 'later', 'local-only commit after push')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    sibling = _mk(project_id='p1', worktree_branch='feat/local')
    remote_sibling = _mk(project_id='p1', worktree_branch='feat/remote')
    main_sibling = _mk(project_id='p1', worktree_branch='main')
    pr = _checkout_pull(12, state, sha)
    sync, broadcast = _quiet_sync()
    try:
        with patch('app.api.git_helpers.get_working_dir_async',
                   new=AsyncMock(return_value=str(pr_checkout))), \
                patch('app.core.github_service.github_service') as gh:
            gh.get_repo_from_remote.return_value = 'o/r'
            gh.list_pulls.side_effect = [[], [pr]]
            assert await reconcile_session_pr(sid)
            assert gh.list_pulls.call_args_list == [
                call('o/r', state='all', limit=100, head='feat/local'),
                call('o/r', state='all', limit=100, head='feat/remote'),
            ]
            gh.list_commit_pulls.assert_not_called()
    finally:
        sync.stop()
    row = database.get_session(sid)
    assert (row['pr_number'], row['pr_state']) == (12, state.lower())
    assert row['worktree_branch'] == 'feat/local'
    assert database.get_session(sibling)['pr_number'] == 12
    assert database.get_session(remote_sibling)['pr_number'] is None
    assert database.get_session(main_sibling)['pr_number'] is None
    entries = database.decode_pull_requests(row['pull_requests'])
    assert entries[0]['branch'] == 'feat/remote'
    payload = broadcast.broadcast_user_session_updated.await_args.kwargs['updates']
    assert payload['pr_state'] == state.lower()
    assert payload['pull_requests'][0]['branch'] == 'feat/remote'


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['OPEN', 'MERGED'])
async def test_reconcile_finds_head_pushed_without_upstream(mock_db, pr_checkout, state):
    from tests.git_repo_helpers import commit_file, git

    sha = commit_file(pr_checkout, 'feature.txt', 'feature', 'feature')
    git(pr_checkout, 'branch', '--unset-upstream')
    git(pr_checkout, 'push', '-q', 'origin', 'HEAD:feat/remote')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    # The commit can be in several PRs: prefer open, else the highest number.
    prs = [_checkout_pull(99, 'MERGED', sha), _checkout_pull(12, state, sha)]
    chosen = 12 if state == 'OPEN' else 99
    sync, _ = _quiet_sync()
    try:
        with patch('app.api.git_helpers.get_working_dir_async',
                   new=AsyncMock(return_value=str(pr_checkout))), \
                patch('app.core.github_service.github_service') as gh:
            gh.get_repo_from_remote.return_value = 'o/r'
            gh.list_pulls.return_value = []
            gh.list_commit_pulls.return_value = prs
            assert await reconcile_session_pr(sid)
            gh.list_commit_pulls.assert_called_once_with('o/r', sha)
    finally:
        sync.stop()
    row = database.get_session(sid)
    assert (row['pr_number'], row['pr_state']) == (chosen, state.lower())
    assert database.decode_pull_requests(row['pull_requests'])[0]['branch'] == 'feat/remote'


@pytest.mark.asyncio
async def test_reconcile_fresh_worktree_does_not_adopt_main_tip_pr(mock_db, pr_checkout):
    from tests.git_repo_helpers import git

    sha = git(pr_checkout, 'rev-parse', 'HEAD')
    assert sha == git(pr_checkout, 'rev-parse', 'origin/main')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    # A squash merge is associated with main's tip, but its PR head is different.
    unrelated = _checkout_pull(99, 'MERGED', 'a' * 40, head='feat/unrelated')
    with patch('app.api.git_helpers.get_working_dir_async',
               new=AsyncMock(return_value=str(pr_checkout))), \
            patch('app.core.github_service.github_service') as gh:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = []
        gh.list_commit_pulls.return_value = [unrelated]
        assert await reconcile_session_pr(sid)
        gh.list_commit_pulls.assert_not_called()
    row = database.get_session(sid)
    assert row['pr_number'] is None
    assert database.decode_pull_requests(row['pull_requests']) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['upstream', 'commit'])
async def test_reconcile_fallback_failure_keeps_stored_state(mock_db, pr_checkout, failure):
    from app.core.github_service import GitHubCliError
    from tests.git_repo_helpers import commit_file, git

    commit_file(pr_checkout, 'feature.txt', 'feature', 'feature')
    if failure == 'upstream':
        git(pr_checkout, 'push', '-q', '-u', 'origin', 'HEAD:feat/remote')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    database.update_session_pr_state(sid, pr_number=4, pr_state='merged')
    with patch('app.api.git_helpers.get_working_dir_async',
               new=AsyncMock(return_value=str(pr_checkout))), \
            patch('app.core.github_service.github_service') as gh:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.side_effect = [[], GitHubCliError('offline')] if failure == 'upstream' else None
        gh.list_pulls.return_value = []
        gh.list_commit_pulls.side_effect = GitHubCliError('offline')
        assert not await reconcile_session_pr(sid)
    assert database.get_session(sid)['pr_number'] == 4
    assert database.get_session(sid)['pr_state'] == 'merged'


@pytest.mark.asyncio
@pytest.mark.parametrize('upstream', ['feat/local', 'feat/unused'])
async def test_reconcile_commit_fallback_after_upstream_miss(mock_db, pr_checkout, upstream):
    from tests.git_repo_helpers import commit_file, git

    sha = commit_file(pr_checkout, 'feature.txt', 'feature', 'feature')
    git(pr_checkout, 'push', '-q', '-u', 'origin', f'HEAD:{upstream}')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    pr = _checkout_pull(12, 'OPEN', sha)
    sync, _ = _quiet_sync()
    try:
        with patch('app.api.git_helpers.get_working_dir_async',
                   new=AsyncMock(return_value=str(pr_checkout))), \
                patch('app.core.github_service.github_service') as gh:
            gh.get_repo_from_remote.return_value = 'o/r'
            gh.list_pulls.return_value = []
            gh.list_commit_pulls.return_value = [pr]
            assert await reconcile_session_pr(sid)
            expected = [call('o/r', state='all', limit=100, head='feat/local')]
            if upstream != 'feat/local':
                expected.append(call('o/r', state='all', limit=100, head=upstream))
            assert gh.list_pulls.call_args_list == expected
            gh.list_commit_pulls.assert_called_once_with('o/r', sha)
    finally:
        sync.stop()
    assert database.get_session(sid)['pr_number'] == 12


@pytest.mark.asyncio
async def test_reconcile_commit_pr_targeting_local_branch_is_ignored(mock_db, pr_checkout):
    from tests.git_repo_helpers import commit_file

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    pr = _checkout_pull(12, 'OPEN', sha)
    pr.base_branch = 'feat/local'
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    with patch('app.api.git_helpers.get_working_dir_async',
               new=AsyncMock(return_value=str(pr_checkout))), \
            patch('app.core.github_service.github_service') as gh:
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = []
        gh.list_commit_pulls.return_value = [pr]
        assert await reconcile_session_pr(sid)
    assert database.get_session(sid)['pr_number'] is None


@pytest.mark.asyncio
async def test_reconcile_fallback_git_calls_are_bounded_and_off_loop(mock_db, pr_checkout):
    import threading
    from app.core.git_service import git_service
    from tests.git_repo_helpers import commit_file

    commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    loop_thread = threading.get_ident()
    run_git = git_service._run_git
    calls = []

    def checked_git(working_dir, args, **kwargs):
        assert threading.get_ident() != loop_thread
        assert kwargs['timeout'] == 5
        calls.append(args)
        return run_git(working_dir, args, **kwargs)

    def commit_pulls(*args):
        assert threading.get_ident() != loop_thread
        return []

    with patch('app.api.git_helpers.get_working_dir_async',
               new=AsyncMock(return_value=str(pr_checkout))), \
            patch('app.core.github_service.github_service') as gh, \
            patch.object(git_service, '_run_git', side_effect=checked_git):
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = []
        gh.list_commit_pulls.side_effect = commit_pulls
        assert await reconcile_session_pr(sid)
    assert ['rev-parse', 'HEAD'] in calls
    assert ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'] in calls
    assert any(args[:2] == ['merge-base', '--is-ancestor'] for args in calls)
    gh.list_commit_pulls.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_git_timeout_returns_false(mock_db, pr_checkout):
    import subprocess
    from app.core.git_service import git_service

    sid = _mk(project_id='p1', worktree_branch='feat/local')
    database.update_session_pr_state(sid, pr_number=4, pr_state='merged')
    with patch('app.api.git_helpers.get_working_dir_async',
               new=AsyncMock(return_value=str(pr_checkout))), \
            patch('app.core.github_service.github_service') as gh, \
            patch.object(git_service, '_run_git', side_effect=subprocess.TimeoutExpired('git', 5)):
        gh.get_repo_from_remote.return_value = 'o/r'
        gh.list_pulls.return_value = []
        assert not await reconcile_session_pr(sid)
        gh.list_commit_pulls.assert_not_called()
    assert database.get_session(sid)['pr_number'] == 4


async def _reconcile_checkout(sid, checkout, pulls=(), commit_pulls=()):
    sync, _ = _quiet_sync()
    try:
        with patch('app.api.git_helpers.get_working_dir_async',
                   new=AsyncMock(return_value=str(checkout))), \
                patch('app.core.github_service.github_service') as gh:
            gh.get_repo_from_remote.return_value = 'o/r'
            gh.list_pulls.side_effect = lambda repo, **kw: [
                pr for pr in pulls if pr.head_branch == kw['head']
            ]
            gh.list_commit_pulls.return_value = list(commit_pulls)
            ok = await reconcile_session_pr(sid)
    finally:
        sync.stop()
    return ok, gh


@pytest.mark.asyncio
@pytest.mark.parametrize('own_commit', [False, True])
async def test_main_tracking_worktree_never_adopts_main_pr(mock_db, pr_checkout, own_commit):
    from tests.git_repo_helpers import commit_file, git

    assert git(pr_checkout, 'rev-parse', '--abbrev-ref', '@{u}') == 'origin/main'
    if own_commit:
        commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    sha = git(pr_checkout, 'rev-parse', 'HEAD')
    main_pr = _checkout_pull(77, 'OPEN', sha, head='main')
    main_pr.base_branch = 'release'
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    main = _mk(project_id='p1', worktree_branch='main')
    ok, gh = await _reconcile_checkout(sid, pr_checkout, [main_pr], [main_pr])
    assert ok
    assert database.get_session(sid)['pr_number'] is None
    assert database.get_session(main)['pr_number'] is None
    assert all(c.kwargs['head'] != 'main' for c in gh.list_pulls.call_args_list)
    if not own_commit:
        gh.list_commit_pulls.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_worker_at_parent_pr_tip_does_not_adopt_it(mock_db, pr_checkout):
    from tests.git_repo_helpers import commit_file, git

    sha = commit_file(pr_checkout, 'parent.txt', 'parent work', 'parent commit')
    parent = _mk(project_id='p1', worktree_branch='feat/local')
    database.update_session_pr_state(parent, pr_number=77, pr_state='open')
    worker_dir = pr_checkout.parent / 'worker'
    git(pr_checkout, 'worktree', 'add', '-q', '-b', 'task/fresh', str(worker_dir), sha)
    worker = _mk(project_id='p1', worktree_branch='task/fresh', parent_session_id=parent, is_worker=True)
    pr = _checkout_pull(77, 'OPEN', sha)
    ok, gh = await _reconcile_checkout(worker, worker_dir, commit_pulls=[pr])
    assert ok
    assert database.get_session(worker)['pr_number'] is None
    assert database.get_session(parent)['pr_number'] == 77
    gh.list_commit_pulls.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['fork', 'unknown-repo', 'head-is-base', 'creation-tip', 'divergent'])
async def test_fallback_rejects_unowned_candidates(mock_db, pr_checkout, invalid):
    from tests.git_repo_helpers import commit_file, git

    creation = git(pr_checkout, 'rev-parse', 'HEAD')
    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    git(pr_checkout, 'push', '-q', '-u', 'origin', 'HEAD:feat/remote')
    pr = _checkout_pull(12, 'OPEN', sha)
    if invalid == 'fork':
        pr.head_repo = 'someone/fork'
    elif invalid == 'unknown-repo':
        pr.head_repo = None
    elif invalid == 'head-is-base':
        pr.base_branch = pr.head_branch
    elif invalid == 'creation-tip':
        pr.head_sha = creation
    else:
        git(pr_checkout, 'checkout', '-q', '-b', 'other', creation)
        pr.head_sha = commit_file(pr_checkout, 'other.txt', 'other work', 'other commit')
        git(pr_checkout, 'checkout', '-q', 'feat/local')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    ok, _ = await _reconcile_checkout(sid, pr_checkout, [pr], [pr])
    assert ok
    assert database.get_session(sid)['pr_number'] is None


@pytest.mark.asyncio
async def test_fallback_without_creation_reflog_skips_github(mock_db, pr_checkout):
    from tests.git_repo_helpers import commit_file, git

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    git(pr_checkout, 'reflog', 'expire', '--expire=all', 'refs/heads/feat/local')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    ok, gh = await _reconcile_checkout(sid, pr_checkout, commit_pulls=[_checkout_pull(12, 'OPEN', sha)])
    assert ok
    gh.list_commit_pulls.assert_not_called()
    assert database.get_session(sid)['pr_number'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['sibling', 'parent', 'worker', 'nested-worker', 'fork', 'archived', 'other-project'])
async def test_fallback_primary_claim_guard(mock_db, pr_checkout, owner):
    from tests.git_repo_helpers import commit_file

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    options = {'project_id': 'p1', 'worktree_branch': 'other'}
    if owner in ('worker', 'fork'):
        options.update(parent_session_id=sid, is_worker=owner == 'worker')
    elif owner == 'nested-worker':
        middle = _mk(project_id='p1', parent_session_id=sid, is_worker=True)
        options.update(parent_session_id=middle, is_worker=True)
    elif owner == 'other-project':
        options['project_id'] = 'p2'
    other = _mk(**options)
    if owner == 'parent':
        child = _mk(project_id='p1', worktree_branch='feat/local', parent_session_id=other, is_worker=True)
        sid = child
    if owner == 'archived':
        database.update_session(other, status='archived')
    database.update_session_pr_state(other, pr_number=12, pr_state='open')
    pr = _checkout_pull(12, 'OPEN', sha)
    ok, _ = await _reconcile_checkout(sid, pr_checkout, commit_pulls=[pr])
    assert ok
    allowed = owner in ('worker', 'nested-worker', 'archived', 'other-project')
    assert database.get_session(sid)['pr_number'] == (12 if allowed else None)


@pytest.mark.asyncio
async def test_fallback_negative_cache_changes_and_expiry(pr_checkout):
    import asyncio
    from unittest.mock import Mock
    from app.core.session_pr_state import find_pull_for_checkout_fallback, _fallback_misses
    from tests.git_repo_helpers import commit_file, git

    commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    gh = Mock()
    gh.list_pulls.return_value = []
    gh.list_commit_pulls.return_value = []

    async def lookup():
        return await asyncio.to_thread(find_pull_for_checkout_fallback, gh, 'o/r', 'feat/local', str(pr_checkout))

    assert await lookup() is None
    assert await lookup() is None
    assert gh.list_commit_pulls.call_count == 1
    commit_file(pr_checkout, 'later.txt', 'later', 'later commit')
    assert await lookup() is None
    assert gh.list_commit_pulls.call_count == 2  # HEAD changed
    git(pr_checkout, 'push', '-q', '-u', 'origin', 'HEAD:feat/remote')
    assert await lookup() is None
    assert gh.list_commit_pulls.call_count == 3  # upstream changed, same HEAD
    for key in _fallback_misses.keys():
        if key[0] == str(pr_checkout):
            _fallback_misses.peek(key).expires_at = 0
    assert await lookup() is None
    assert gh.list_commit_pulls.call_count == 4


@pytest.mark.asyncio
async def test_fallback_failures_are_not_negative_cached(pr_checkout):
    import asyncio
    from unittest.mock import Mock
    from app.core.github_service import GitHubCliError
    from app.core.session_pr_state import find_pull_for_checkout_fallback
    from tests.git_repo_helpers import commit_file

    commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    gh = Mock()
    gh.list_commit_pulls.side_effect = GitHubCliError('offline')
    for _ in range(2):
        with pytest.raises(GitHubCliError):
            await asyncio.to_thread(find_pull_for_checkout_fallback, gh, 'o/r', 'feat/local', str(pr_checkout))
    assert gh.list_commit_pulls.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('lookup', ['upstream', 'commit'])
async def test_fallback_skips_unfetched_head_and_keeps_valid_candidate(mock_db, pr_checkout, lookup):
    from tests.git_repo_helpers import commit_file, git

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    if lookup == 'upstream':
        git(pr_checkout, 'push', '-q', '-u', 'origin', 'HEAD:feat/remote')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    unknown = _checkout_pull(99, 'OPEN', 'b' * 40)
    valid = _checkout_pull(12, 'OPEN', sha)
    candidates = [unknown, valid]
    ok, _ = await _reconcile_checkout(
        sid, pr_checkout,
        pulls=candidates if lookup == 'upstream' else (),
        commit_pulls=candidates if lookup == 'commit' else (),
    )
    assert ok  # an unfetched PR head is not a GitHub/connectivity failure
    assert database.get_session(sid)['pr_number'] == valid.number


@pytest.mark.asyncio
async def test_forced_turn_end_reconcile_bypasses_cached_fallback_miss(mock_db, pr_checkout):
    from app.core.session_pr_state import schedule_session_pr_reconcile
    from tests.git_repo_helpers import commit_file

    sha = commit_file(pr_checkout, 'own.txt', 'own work', 'session commit')
    sid = _mk(project_id='p1', worktree_branch='feat/local')
    pr = _checkout_pull(12, 'OPEN', sha)
    sync, _ = _quiet_sync()
    try:
        with patch('app.api.git_helpers.get_working_dir_async',
                   new=AsyncMock(return_value=str(pr_checkout))), \
                patch('app.core.github_service.github_service') as gh:
            gh.get_repo_from_remote.return_value = 'o/r'
            gh.list_pulls.return_value = []
            gh.list_commit_pulls.return_value = []
            assert await reconcile_session_pr(sid)  # header/poll after push, before gh pr create
            gh.list_commit_pulls.return_value = [pr]  # gh pr create: unchanged HEAD/upstream
            assert await reconcile_session_pr(sid)  # ordinary polls still reuse the miss
            assert gh.list_commit_pulls.call_count == 1
            assert database.get_session(sid)['pr_number'] is None
            await schedule_session_pr_reconcile(sid, force=True)
            assert database.get_session(sid)['pr_number'] == pr.number
            assert gh.list_commit_pulls.call_count == 2
    finally:
        sync.stop()

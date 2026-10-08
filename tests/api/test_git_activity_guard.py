from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api import git_operations, git_owner, github
from app.core import git_activity as activity


@pytest.fixture
def target(monkeypatch, tmp_path):
    here = tmp_path / 'checkout'
    here.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    sessions = [{'id': 'busy', 'project_id': 'legacy', 'project_v2_id': 'pv2', 'branch': 'feature'}]
    monkeypatch.setattr(activity, 'get_checkout_by_path', lambda _: {'project_id': 'pv2'})
    monkeypatch.setattr(activity, 'resolve_project_v2', lambda _: {'id': 'pv2'})
    listing = Mock(side_effect=lambda *args: sessions)
    monkeypatch.setattr(activity, 'list_sessions_for_cleanup', listing)
    monkeypatch.setattr(activity, '_live_session_ids', lambda: {'busy'})
    monkeypatch.setattr(activity.task_registry, 'has_active_background_work', lambda _: False)
    monkeypatch.setattr(activity.control, 'owner_of', lambda _: None)
    monkeypatch.setattr(activity, 'resolve_existing_cwd', lambda _: str(other))
    monkeypatch.setattr(activity.git_service, 'get_current_branch', lambda path: 'feature' if path == str(here) else sessions[0]['branch'] or 'feature')
    return str(here), str(other), sessions, listing

def test_same_branch_different_directory_is_busy(target):
    here, _, _, listing = target
    assert activity.busy_git_target_sessions('legacy', here) == ['busy']
    listing.assert_called_with('legacy', 'pv2')

def test_same_directory_different_branch_metadata_is_busy(target, monkeypatch):
    here, _, sessions, _ = target
    sessions[0]['branch'] = 'stale-metadata'
    monkeypatch.setattr(activity, 'resolve_existing_cwd', lambda _: here)
    assert activity.busy_git_target_sessions('legacy', here) == ['busy']

def test_main_checkout_branch_without_metadata_is_busy(target):
    here, _, sessions, _ = target
    sessions[0]['branch'] = None
    assert activity.busy_git_target_sessions('legacy', here) == ['busy']

def test_unrelated_known_branch_directory_remains_idle(target, monkeypatch):
    here, _, sessions, _ = target
    sessions[0]['branch'] = 'other-feature'
    assert activity.busy_git_target_sessions('legacy', here) == []

def test_idle_no_longer_locks_after_lease_release(target, monkeypatch):
    monkeypatch.setattr(activity, '_live_session_ids', lambda: set())
    assert activity.busy_git_target_sessions('legacy', target[0]) == []

def test_background_work_without_model_lease_is_busy(target, monkeypatch):
    monkeypatch.setattr(activity, '_live_session_ids', lambda: set())
    monkeypatch.setattr(activity.task_registry, 'has_active_background_work', lambda _: True)
    assert activity.busy_git_target_sessions('legacy', target[0]) == ['busy']

def test_monitor_background_work_without_model_lease_is_busy(target, monkeypatch, mock_db):
    from app.core.task_registry import TaskRegistry
    from app.db import database

    database.create_session('busy', 'default')
    registry = TaskRegistry()
    monkeypatch.setattr(activity, 'task_registry', registry)
    monkeypatch.setattr(activity, '_live_session_ids', lambda: set())
    registry.start_task('busy', 'monitor-only', 'shell', 'Working')
    registry.mark_monitor_shell_task('busy', 'monitor-only')
    assert not registry.has_active_background_work('busy')  # Monitor is not SDK work.
    assert activity.busy_git_target_sessions('legacy', target[0]) == ['busy']
    registry.complete_task('busy', 'monitor-only', 'completed', notify=False)
    assert activity.busy_git_target_sessions('legacy', target[0]) == []


def test_explicit_destination_branch_is_protected(target, monkeypatch):
    monkeypatch.setattr(activity.git_service, 'get_current_branch', lambda _: 'unrelated')
    assert activity.busy_git_target_sessions('legacy', target[0], ['feature']) == ['busy']

def test_remote_same_branch_is_protected_without_local_cwd(target, monkeypatch):
    monkeypatch.setattr(activity.control, 'owner_of', lambda _: 'other-worker')
    assert activity.busy_git_target_sessions('legacy', target[0]) == ['busy']

def test_excluded_sessions_are_skipped_but_others_still_count(target, monkeypatch):
    # A handoff accounts for its own reservations; an unknown-cwd remote session
    # it excluded must not make the check fail closed.
    here, _, sessions, _ = target
    sessions[0]['branch'] = None
    monkeypatch.setattr(activity.control, 'owner_of', lambda _: 'other-worker')
    assert activity.busy_git_target_sessions('legacy', here, exclude=('busy',)) == []
    sessions.append({'id': 'second', 'project_id': 'legacy', 'branch': 'feature'})
    monkeypatch.setattr(activity, '_live_session_ids', lambda: {'busy', 'second'})
    assert activity.busy_git_target_sessions('legacy', here, exclude=('busy',)) == ['second']

def test_activity_errors_fail_closed(target, monkeypatch):
    monkeypatch.setattr(activity, '_live_session_ids', Mock(side_effect=RuntimeError('DB offline')))
    assert activity.git_target_mutations_blocked('legacy', target[0]) is True
    with pytest.raises(HTTPException) as error:
        activity.require_idle_git_target('legacy', target[0])
    assert error.value.status_code == 503

def test_unknown_busy_cwd_fail_closed(target, monkeypatch):
    target[2][0]['branch'] = None
    monkeypatch.setattr(activity, 'resolve_existing_cwd', lambda _: None)
    with pytest.raises(HTTPException) as error:
        activity.require_idle_git_target('legacy', target[0])
    assert error.value.status_code == 503

@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(git_owner._MUTATING_ACTIONS))
async def test_every_owner_mutation_checks_idle_before_git(name, monkeypatch):
    monkeypatch.setattr('app.api.common.check_project_access', lambda *a: None)
    monkeypatch.setattr('app.api.git_helpers.get_working_dir_async', AsyncMock(return_value='/checkout'))
    check = Mock(side_effect=HTTPException(409, 'busy'))
    monkeypatch.setattr(activity, 'require_idle_git_target', check)
    with pytest.raises(HTTPException) as error:
        await git_owner._guard_mutation(name, {'request': object(), 'project_id': 'p', 'body': None})
    assert error.value.status_code == 409
    check.assert_called_once()

@pytest.mark.asyncio
async def test_checkout_checks_the_requested_ref(monkeypatch):
    monkeypatch.setattr('app.api.common.check_project_access', lambda *a: None)
    monkeypatch.setattr('app.api.git_helpers.get_working_dir_async', AsyncMock(return_value='/checkout'))
    check = Mock()
    monkeypatch.setattr(activity, 'require_idle_git_target', check)
    await git_owner._guard_mutation('checkout_branch', {'request': object(), 'project_id': 'p', 'body': SimpleNamespace(ref='other-active')})
    assert 'other-active' in check.call_args.args[2]

@pytest.mark.asyncio
async def test_read_only_action_does_not_resolve_or_lock_git(monkeypatch):
    resolve = AsyncMock(side_effect=AssertionError('read must remain available'))
    monkeypatch.setattr('app.api.git_helpers.get_working_dir_async', resolve)
    await git_owner._guard_mutation('get_file_diff', {})
    resolve.assert_not_called()

@pytest.mark.asyncio
async def test_merge_checks_base_branch_before_github_write(monkeypatch):
    monkeypatch.setattr(activity, 'project_has_active_git_sessions', lambda *args: True)
    monkeypatch.setattr(github.github_service, 'get_pull', lambda *a: SimpleNamespace(head_branch='feature', base_branch='main'))
    check = Mock(side_effect=HTTPException(409, 'base active'))
    monkeypatch.setattr(activity, 'require_idle_git_target', check)
    with pytest.raises(HTTPException):
        await github.require_idle_pull_branches('p', 'owner/repo', 1)
    check.assert_called_once_with('p', None, ('feature', 'main'))

@pytest.mark.asyncio
async def test_queued_merge_rechecks_activity_before_write(monkeypatch):
    monkeypatch.setattr(github, 'require_idle_pull_branches', AsyncMock(side_effect=HTTPException(409, 'busy')))
    merge = Mock(side_effect=AssertionError('must not merge'))
    monkeypatch.setattr(git_operations.github_service, 'merge_pull', merge)
    op = git_operations.Operation(id='operation', project_id='p', kind='finish', steps=[git_operations._step('merge', 'Merge pull request')])
    assert await git_operations._merge_step(op, 'owner/repo', 1, 'squash') is False
    assert op.status == 'error'
    merge.assert_not_called()

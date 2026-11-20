"""A worker folder edit is a guarded base registration update, not a move."""
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import project_workers
from app.core import project_folders, project_resolver, project_setup
from app.core.config import settings
from app.db import checkouts, database, projects, projects_v2, turns
from app.network import control, engine
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401


@pytest.fixture
def registered(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), 'effective_workspace_dir', property(lambda _: tmp_path))
    monkeypatch.setattr('app.api.projects.is_local_mode', lambda: False)
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir()
    new.mkdir()
    (old / 'old-only').write_text('keep me')
    (new / 'new-only').write_text('leave me')
    row = database.create_project('moving', 'Moving', None, 'old')
    identity = projects_v2.get_project_v2_by_origin('local:moving')
    return row, identity['id'], old, new


def test_retargets_only_local_base_and_chat_identity(registered):
    row, identity, old, new = registered
    base = checkouts.get_checkout(identity, ME)
    peer = checkouts.upsert_checkout(identity, 'other', str(old))
    branch = checkouts.upsert_checkout(identity, ME, str(old / 'topic'), branch='topic')
    database.create_session('before', 'default', project_id='moving')
    before = database.get_session('before')
    revision = project_folders.revision(row)

    changed = project_setup.folder_action('change-folder', {
        'project_v2_id': identity, 'folder': str(new), 'expected_revision': revision,
    })
    assert changed['folder'] == str(new)
    assert changed['folder_revision'] != revision
    assert database.get_project('moving')['path'] == 'new'
    assert (checkouts.get_checkout(identity, ME)['id'], checkouts.get_checkout(identity, ME)['path']) == (base['id'], str(new))
    assert checkouts.get_checkout(identity, ME, branch='topic') == branch
    assert checkouts.get_checkout(identity, 'other') == peer
    assert database.get_session('before') == before
    assert project_resolver.resolve_existing_cwd(before) == str(new)
    database.create_session('after', 'default', project_id='moving', project_v2_id=identity)
    after = database.get_session('after')
    assert after['project_v2_id'] == identity
    assert project_resolver.resolve_cwd(after) == str(new)
    assert (old / 'old-only').read_text() == 'keep me'
    assert (new / 'new-only').read_text() == 'leave me'
    assert not (new / 'old-only').exists()
    stale = project_setup.folder_action('change-folder', {
        'project_v2_id': identity, 'folder': str(old), 'expected_revision': revision,
    })
    assert stale['status'] == 409
    assert database.get_project(row['id'])['path'] == 'new'


@pytest.mark.parametrize('path', ['', 'missing', 'old/old-only', '../escaping', 'outside-link', 'nul\x00path'])
def test_refuses_non_directories_and_workspace_escape(registered, tmp_path, path):
    row, identity, old, _ = registered
    (tmp_path / 'outside-link').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        project_folders.retarget(row, path, project_folders.revision(row))
    assert error.value.status_code == 400
    assert checkouts.get_checkout(identity, ME)['path'] == 'old'
    assert (old / 'old-only').exists()


def test_refuses_a_stale_database_snapshot_even_if_validation_already_ran(registered):
    row, identity, old, new = registered
    database.update_project(row['id'], name='Edited elsewhere')
    with pytest.raises(projects.FolderConflict, match='changed'):
        projects.retarget_folder(row, old_path=str(old), new_path=str(new), stored_path='new', same_folder=False)
    assert checkouts.get_checkout(identity, ME)['path'] == 'old'


def test_setup_relative_and_absolute_twins_retarget_as_one(registered):
    row, identity, old, new = registered
    canonical = checkouts.upsert_checkout(identity, ME, str(old))
    project_folders.retarget(row, str(new), project_folders.revision(row))
    bases = checkouts.list_checkouts(identity)
    assert [(base['id'], base['path']) for base in bases] == [(canonical['id'], str(new))]


def test_repair_then_edit_with_noncanonical_legacy_path(registered):
    row, identity, old, new = registered
    # Reproduce the row an older worker could write before startup repair.
    (old.parent / 'existing').mkdir()
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE projects SET path = 'existing/../old' WHERE id = %s", (row['id'],))
        cursor.execute("UPDATE checkouts SET path = 'existing/../old' WHERE project_id = %s", (identity,))
    assert checkouts.repair_relative_checkout_paths(ME, old.parent)['rewritten'] == 1
    row = database.get_project(row['id'])
    assert row['path'] == 'existing/../old'
    assert checkouts.get_checkout(identity, ME)['path'] == str(old)
    assert project_folders.retarget(row, str(new), project_folders.revision(row))['path'] == 'new'


@pytest.mark.parametrize('pre_upgrade', [False, True])
def test_remove_setup_different_folder_and_edit(registered, pre_upgrade):
    row, identity, old, new = registered
    original = checkouts.upsert_checkout(identity, ME, str(old))
    checkouts.repair_relative_checkout_paths(ME, old.parent)
    branch = checkouts.upsert_checkout(identity, ME, str(old / 'topic'), branch='topic')
    peer = checkouts.upsert_checkout(identity, 'other', str(old.parent / 'peer'))
    assert project_setup.remove_local(identity)['removed'] == [row['id']]
    assert checkouts.get_checkout(identity, ME)['status'] == 'removed'
    setup = project_setup.execute_setup({
        'id': 're-setup', 'project_v2_id': identity,
        'params': {'origin': 'local:moving', 'kind': 'folder', 'folder': 'new'},
    })
    row = database.get_project(setup['project_id'])
    retained = checkouts.get_checkout_by_path(str(old), project_id=identity, runner_id=ME)
    assert retained['status'] == 'removed'
    if pre_upgrade:
        with database.get_db() as conn:
            conn.cursor().execute("UPDATE checkouts SET status = 'active' WHERE id = %s", (original['id'],))
    checkouts.repair_relative_checkout_paths(ME, old.parent)
    chat = {'project_v2_id': identity}
    assert project_resolver.resolve_existing_cwd(chat) == str(new)
    assert project_resolver.resolve_cwd(chat) == str(new)
    third = old.parent / 'third'
    third.mkdir()
    assert project_folders.retarget(row, str(third), project_folders.revision(row))['path'] == 'third'
    assert project_resolver.resolve_existing_cwd(chat) == str(third)
    retained = checkouts.get_checkout_by_path(str(old), project_id=identity, runner_id=ME)
    assert (retained['id'], retained['path'], retained['status']) == (original['id'], original['path'], 'removed')
    assert (old / 'old-only').read_text() == 'keep me'
    assert (new / 'new-only').read_text() == 'leave me'
    assert checkouts.get_checkout(identity, ME, branch='topic') == branch
    assert checkouts.get_checkout(identity, 'other') == peer


@pytest.fixture
def synced_worktree(registered):
    from app.core.project_links import auto_link_group_id
    from app.core.worktree_manager import WorktreeManager

    row, _, old, new = registered
    def git(path, *args):
        return subprocess.run(['git', '-C', str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    origin = old.parent / 'origin'
    origin.mkdir()
    git(origin, 'init', '-b', 'main')
    git(origin, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
        'commit', '--allow-empty', '-m', 'initial')
    for dest in (old, new):
        clone = dest.with_name(dest.name + '-clone')
        git(origin, 'clone', str(origin), str(clone))
        (clone / '.git').rename(dest / '.git')
    topic = old.parent / 'external-topic'
    git(old, 'worktree', 'add', '-b', 'topic', str(topic))
    row = database.set_project_link_group(row['id'], auto_link_group_id(str(origin)))
    repo = database.create_git_repository('repo', row['id'], str(origin))
    assert WorktreeManager().sync_worktrees_for_project(row['id'])['imported'] == 1
    worktree = database.get_active_worktrees_for_repository(repo['id'])[0]
    assert worktree['worktree_path'] == str(topic)
    return row, old, new, topic, worktree, git


@pytest.mark.asyncio
@pytest.mark.parametrize('pre_upgrade', [False, True])
async def test_imported_worktree_cleanup_keeps_original_clone(synced_worktree, pre_upgrade, monkeypatch):
    from app.core.worktree_manager import WorktreeError, WorktreeManager

    row, old, new, topic, worktree, git = synced_worktree
    wid = worktree['id']
    if pre_upgrade:
        with database.get_db() as conn:
            conn.cursor().execute('UPDATE worktrees SET cleanup_owner = NULL WHERE id = %s', (wid,))
    # Same-named branch on the new clone must not be deleted.
    git(new, 'branch', 'topic')
    assert project_folders.retarget(row, str(new), project_folders.revision(row))['path'] == 'new'
    manager = WorktreeManager()  # Fresh manager: proof is persisted, not cached.
    assert manager.sync_worktrees_for_project(row['id'])['active'] == 1
    other_topic = new.parent / 'from-new-clone'
    git(new, 'worktree', 'add', '-b', 'other-topic', str(other_topic))
    assert manager.sync_worktrees_for_project(row['id'])['imported'] == 1
    moved = old.rename(old.with_name('original-unavailable'))
    with pytest.raises(WorktreeError, match='Cleanup refused'):
        await manager.cleanup_worktree(wid, keep_branch=False)
    moved.rename(old)
    with monkeypatch.context() as other_runner:
        other_runner.setattr(type(settings), 'effective_instance_id', property(lambda _: 'foreign-runner'))
        with pytest.raises(WorktreeError, match='original owner could not be verified'):
            await manager.cleanup_worktree(wid, keep_branch=False)
    admin = git(topic, 'rev-parse', '--absolute-git-dir')
    backlink = Path(admin) / 'gitdir'
    saved = backlink.read_bytes()
    backlink.write_text(str(new / '.git'))
    with pytest.raises(WorktreeError, match='back-reference'):
        await manager.cleanup_worktree(wid, keep_branch=False)
    assert topic.is_dir()
    assert database.get_worktree(wid)['status'] == 'active'
    backlink.write_bytes(saved)
    assert await manager.cleanup_worktree(wid, keep_branch=False)
    assert not topic.exists()
    assert git(old, 'branch', '--list', 'topic') == ''
    assert git(new, 'branch', '--list', 'topic') == 'topic'
    assert other_topic.is_dir()
    assert database.get_worktree(wid)['status'] == 'completed'


def test_refuses_retarget_with_unprovable_legacy_worktree(synced_worktree):
    row, _, new, topic, worktree, _ = synced_worktree
    with database.get_db() as conn:
        conn.cursor().execute('UPDATE worktrees SET cleanup_owner = NULL WHERE id = %s', (worktree['id'],))
    # Replace the linked checkout's .git file with a link to a different clone.
    (topic / '.git').write_text(f'gitdir: {new / ".git"}\n')
    with pytest.raises(HTTPException, match='original Git checkout cannot be verified') as error:
        project_folders.retarget(row, str(new), project_folders.revision(row))
    assert error.value.status_code == 409
    assert database.get_project(row['id'])['path'] == 'old'
    assert topic.is_dir()


def test_folder_routes_require_owner(mock_db):
    app = FastAPI()
    app.include_router(project_workers.router)
    client = TestClient(app)
    url = '/api/v1/projects/workers/peer/folder'
    assert client.get(f'{url}/browse?project_id=moving').status_code == 403
    assert client.put(f'{url}?project_id=moving', json={
        'folder': '/some/path', 'expected_revision': 'a' * 64,
    }).status_code == 403


def test_conflicting_checkout_rolls_back(registered):
    row, identity, _, new = registered
    checkouts.upsert_checkout(identity, ME, str(new), branch='topic')
    with pytest.raises(HTTPException) as error:
        project_folders.retarget(row, str(new), project_folders.revision(row))
    assert error.value.status_code == 409
    assert database.get_project(row['id'])['path'] == 'old'


def test_refuses_queued_chat_on_target(registered):
    row, identity, _, new = registered
    database.create_session('busy', 'default', project_id='moving')
    turns.enqueue_turn('busy', 'user', prompt='Do work')
    with pytest.raises(HTTPException, match='queued and running'):
        project_folders.retarget(row, str(new), project_folders.revision(row))
    assert checkouts.get_checkout(identity, ME)['path'] == 'old'


def test_existing_git_worktree_is_still_resolved_after_retarget(registered):
    row, identity, old, new = registered

    def git(path, *args):
        subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True)

    git(old, 'init', '-b', 'main')
    git(old, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
        'commit', '--allow-empty', '-m', 'initial')
    git(new, 'init', '-b', 'main')
    git(new, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
        'commit', '--allow-empty', '-m', 'initial')
    topic = old / 'existing-topic'
    git(old, 'worktree', 'add', '-b', 'topic', str(topic))
    original = checkouts.upsert_checkout(identity, ME, str(topic), branch='topic')
    chat = {'project_v2_id': identity, 'branch': 'topic'}
    assert project_resolver.resolve_existing_cwd(chat) == str(topic)

    with pytest.raises(HTTPException, match='not a linked Git worktree'):
        project_folders.retarget(row, str(topic), project_folders.revision(row))
    assert project_folders.retarget(row, str(new), project_folders.revision(row))['path'] == 'new'
    assert checkouts.get_checkout(identity, ME, branch='topic') == original
    assert project_resolver.resolve_existing_cwd(chat) == str(topic)
    assert project_resolver.resolve_cwd(chat) == str(topic)


def test_linked_repo_cannot_point_to_different_repository(registered):
    row, _, old, new = registered
    subprocess.run(['git', 'init', str(new)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(new), 'remote', 'add', 'origin', 'https://example.test/wrong/repo'], check=True)
    row = database.set_project_link_group(row['id'], 'git:example.test/right/repo')
    with pytest.raises(HTTPException, match='same Git repository'):
        project_folders.retarget(row, str(new), project_folders.revision(row))
    assert (old / 'old-only').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('online', [True, False])
async def test_peer_route_forwards_raw_target_path_or_refuses_offline(monkeypatch, registered, online):
    _, identity, _, _ = registered
    monkeypatch.setattr(project_workers, '_approved_workers', lambda: [{'id': 'peer', 'online': online}])
    monkeypatch.setattr(project_workers, '_self_install_id', lambda: ME)
    monkeypatch.setattr(engine, 'context', lambda: object())
    path = r'Q:\Work\existing'
    call = AsyncMock(return_value={'folder': path, 'folder_revision': 'b' * 64})
    monkeypatch.setattr(control, 'call', call)
    monkeypatch.setattr(project_setup, 'emit_changed', lambda: None)
    if not online:
        with pytest.raises(HTTPException, match='offline'):
            await project_workers._worker_folder_action(None, 'peer', None, identity, 'change-folder', folder=path)
        call.assert_not_called()
    else:
        result = await project_workers._worker_folder_action(None, 'peer', None, identity, 'change-folder',
                                                             folder=path, expected_revision='a' * 64)
        assert result['folder'] == path
        assert call.await_args.args[2] == {'action': 'change-folder', 'project_v2_id': identity,
                                          'folder': path, 'expected_revision': 'a' * 64}

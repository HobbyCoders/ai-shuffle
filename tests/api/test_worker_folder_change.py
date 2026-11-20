"""A worker folder edit is a guarded base registration update, not a move."""
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import git_worktrees, project_workers
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
@pytest.mark.parametrize('resolve', [project_resolver.resolve_existing_cwd, project_resolver.resolve_cwd])
def test_remove_setup_different_folder_and_edit(registered, pre_upgrade, resolve):
    row, identity, old, new = registered
    original = checkouts.upsert_checkout(identity, ME, str(old))
    checkouts.repair_relative_checkout_paths(ME, old.parent)
    branch = checkouts.upsert_checkout(identity, ME, str(old / 'topic'), branch='topic')
    peer = checkouts.upsert_checkout(identity, 'other', str(old.parent / 'peer'))
    assert project_setup.remove_local(identity)['removed'] == [row['id']]
    assert checkouts.get_checkout(identity, ME)['status'] == 'removed'
    # A chat bound to a truly removed project can still use its retained folder.
    assert resolve({'project_v2_id': identity}) == str(old)
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

    third.rmdir()
    assert resolve(chat) is None
    assert checkouts.get_checkout_by_path(str(old), project_id=identity, runner_id=ME)['status'] == 'removed'


@pytest.fixture
def synced_worktree(registered, request):
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
    database.create_session('before-link', 'default', project_v2_id=projects_v2.get_project_v2_by_origin('local:moving')['id'])
    row = database.set_project_link_group(row['id'], auto_link_group_id(str(origin)))
    repo = database.create_git_repository('repo', row['id'], str(origin))
    manager = WorktreeManager()
    if getattr(request, 'param', 'external') == 'managed':
        topic = old / '.fluid' / 'worktrees' / 'topic'
        seed = database.create_worktree('managed-topic', repo['id'], 'topic', str(topic),
                                        status='completed')
        worktree = manager.recreate_worktree(seed['id'])
    else:
        topic = old.parent / 'external-topic'
        git(old, 'worktree', 'add', '-b', 'topic', str(topic))
        assert manager.sync_worktrees_for_project(row['id'])['imported'] == 1
        worktree = database.get_active_worktrees_for_repository(repo['id'])[0]
        assert worktree['worktree_path'] == str(topic)
    return row, old, new, topic, worktree, git


def test_retarget_first_link_aligns_both_base_chats(synced_worktree):
    row, old, new, _, worktree, _ = synced_worktree
    before = database.get_session('before-link')
    original_id = before['project_v2_id']
    current_id = projects_v2.get_project_v2_by_origin(row['link_group_id'])['id']
    assert original_id != current_id
    database.create_session('after-link', 'default', project_v2_id=current_id)
    after = database.get_session('after-link')
    assert after['project_v2_id'] == current_id
    assert project_resolver.resolve_existing_cwd(before) == str(old)
    peer = checkouts.upsert_checkout(original_id, 'peer', str(old))
    branch = checkouts.upsert_checkout(original_id, ME, worktree['worktree_path'], branch='topic')

    project_folders.retarget(row, str(new), project_folders.revision(row))
    for chat in (before, after):
        assert project_resolver.resolve_existing_cwd(chat) == str(new)
        assert project_resolver.resolve_cwd(chat) == str(new)
        assert database.get_session(chat['id'])['project_v2_id'] == chat['project_v2_id']
    assert checkouts.get_checkout(original_id, ME)['status'] == 'removed'
    assert checkouts.get_checkout(original_id, 'peer') == peer
    assert checkouts.get_checkout(original_id, ME, branch='topic') == branch
    retained = database.get_worktree(worktree['id'])
    assert retained['cleanup_owner'] is not None
    assert {**retained, 'cleanup_owner': None} == worktree
    unavailable = new.rename(new.with_name('current-unavailable'))
    for chat in (before, after):
        assert project_resolver.resolve_existing_cwd(chat) is None
        assert project_resolver.resolve_cwd(chat) is None
    unavailable.rename(new)


@pytest.mark.parametrize('binding', ['original', 'current'])
def test_retarget_first_link_checks_active_shared_only_chats(synced_worktree, binding):
    row, old, new, _, _, _ = synced_worktree
    original_id = database.get_session('before-link')['project_v2_id']
    current_id = projects_v2.get_project_v2_by_origin(row['link_group_id'])['id']
    database.create_session('busy', 'default', project_v2_id=original_id if binding == 'original' else current_id)
    turns.enqueue_turn('busy', 'user', prompt='Do work')
    with pytest.raises(HTTPException, match='queued and running'):
        project_folders.retarget(row, str(new), project_folders.revision(row))
    for identity in (original_id, current_id):
        assert project_resolver.resolve_existing_cwd({'project_v2_id': identity}) == str(old)


def test_retarget_first_link_rolls_back_if_either_identity_has_conflicting_checkout(synced_worktree):
    row, old, new, _, worktree, _ = synced_worktree
    original_id = database.get_session('before-link')['project_v2_id']
    current_id = projects_v2.get_project_v2_by_origin(row['link_group_id'])['id']
    original_checkout = checkouts.upsert_checkout(original_id, ME, str(new), branch='existing')
    with pytest.raises(HTTPException, match='already registered'):
        project_folders.retarget(row, str(new), project_folders.revision(row))
    assert database.get_project(row['id']) == row
    assert database.get_worktree(worktree['id']) == worktree
    assert checkouts.get_checkout(original_id, ME, branch='existing') == original_checkout
    assert project_resolver.resolve_existing_cwd({'project_v2_id': current_id}) == str(old)
    assert project_resolver.resolve_existing_cwd({'project_v2_id': original_id}) == str(old)


def test_retarget_does_not_guess_past_links(synced_worktree):
    from app.core.project_links import auto_link_group_id

    row, old, new, _, _, git = synced_worktree
    original = database.get_session('before-link')
    past_id = projects_v2.get_project_v2_by_origin(row['link_group_id'])['id']
    database.create_session('past-link', 'default', project_id=row['id'], project_v2_id=past_id)
    # This queued chat is explicitly bound to a retired identity, not the base
    # being moved. Its stored legacy pointer alone is not ownership proof.
    turns.enqueue_turn('past-link', 'user', prompt='Use the old folder')
    origin = old.parent / 'second-origin'
    git(old.parent, 'clone', git(old, 'remote', 'get-url', 'origin'), str(origin))
    for clone in (old, new):
        git(clone, 'remote', 'set-url', 'origin', str(origin))
    row = database.set_project_link_group(row['id'], auto_link_group_id(str(origin)))
    other_path = old.parent / 'unrelated'
    other_path.mkdir()
    other = database.create_project('other', 'Other', None, str(other_path))
    current_id = projects_v2.get_project_v2_by_origin(row['link_group_id'])['id']
    project_folders.retarget(row, str(new), project_folders.revision(row))
    assert project_resolver.resolve_cwd(original) == str(old)
    assert project_resolver.resolve_cwd(database.get_session('past-link')) == str(old)
    assert project_resolver.resolve_cwd({'project_v2_id': current_id}) == str(new)
    assert database.get_project(other['id']) == other


def test_retarget_refuses_multiple_local_owners_of_shared_identity(synced_worktree):
    row, old, new, _, _, git = synced_worktree
    sibling = old.parent / 'sibling'
    origin = git(old, 'remote', 'get-url', 'origin')
    git(old.parent, 'clone', origin, str(sibling))
    other = database.create_project('other-clone', 'Other clone', None, str(sibling),
                                    link_group_id=row['link_group_id'])
    with pytest.raises(HTTPException, match='single matching base folder'):
        project_folders.retarget(row, str(new), project_folders.revision(row))
    assert database.get_project(row['id']) == row
    assert database.get_project(other['id']) == other


def test_retarget_back_to_first_link_leaves_stale_alias_base_alone(synced_worktree):
    from app.core.project_links import auto_link_group_id

    row, old, new, _, _, git = synced_worktree
    before = database.get_session('before-link')
    historical = checkouts.get_checkout(before['project_v2_id'], ME)
    first_link = row['link_group_id']
    identity = projects_v2.get_project_v2_by_origin(first_link)['id']
    other_origin = old.parent / 'second-origin'
    git(old.parent, 'clone', git(old, 'remote', 'get-url', 'origin'), str(other_origin))
    for clone in (old, new):
        git(clone, 'remote', 'set-url', 'origin', str(other_origin))
    row = database.set_project_link_group(row['id'], auto_link_group_id(str(other_origin)))
    project_folders.retarget(row, str(new), project_folders.revision(row))

    for clone in (old, new):
        git(clone, 'remote', 'set-url', 'origin', str(old.parent / 'origin'))
    row = database.set_project_link_group(row['id'], first_link)
    destination = old.parent / 'third-clone'
    git(old.parent, 'clone', str(old.parent / 'origin'), str(destination))
    turns.enqueue_turn('before-link', 'user', prompt='Use the historical folder')
    project_folders.retarget(row, str(destination), project_folders.revision(row))
    assert project_resolver.resolve_cwd({'project_v2_id': identity}) == str(destination)
    assert checkouts.get_checkout(before['project_v2_id'], ME) == historical
    assert database.get_session('before-link')['project_v2_id'] == before['project_v2_id']
    assert (old / 'old-only').read_text() == 'keep me'
    assert (new / 'new-only').read_text() == 'leave me'


@pytest.mark.asyncio
@pytest.mark.parametrize('synced_worktree', ['managed', 'external'], indirect=True)
@pytest.mark.parametrize('keep_first', [True, False])
@pytest.mark.parametrize('explicit', [True, False])
async def test_kept_worktree_after_retarget(synced_worktree, keep_first, explicit):
    from app.core.checkout_manager import WorktreeAddError
    from app.core.worktree_manager import WorktreeManager, is_retarget_pinned

    row, old, new, topic, worktree, git = synced_worktree
    wid = worktree['id']
    managed = topic.is_relative_to(old)
    git(new, 'branch', 'topic')  # A matching name in B is not a candidate.
    database.create_session('branch-chat', 'default', project_id=row['id'], worktree_id=wid,
                            worktree_branch='topic')
    chat = database.get_session('branch-chat')
    manager = WorktreeManager()
    if not keep_first:
        (topic / 'keep-local.txt').write_text('keep working here')
        project_folders.retarget(row, str(new), project_folders.revision(row))
        assert project_resolver.resolve_existing_cwd(chat) == str(topic)
        assert project_resolver.resolve_cwd(chat) == str(topic)
        assert (topic / 'keep-local.txt').read_text() == 'keep working here'
    git(topic, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
        'commit', '--allow-empty', '-m', 'only in the original clone')
    retained = git(topic, 'rev-parse', 'HEAD')
    assert await manager.cleanup_worktree(wid, keep_branch=True)
    if keep_first:
        assert not is_retarget_pinned(database.get_worktree(wid))
        project_folders.retarget(row, str(new), project_folders.revision(row))
    completed = database.get_worktree(wid)
    assert is_retarget_pinned(completed)
    assert not topic.exists()
    assert project_resolver.resolve_existing_cwd(chat) is None
    new_refs = git(new, 'show-ref')
    new_listing = git(new, 'worktree', 'list', '--porcelain')
    old_git_paths = set((old / '.git').rglob('*'))
    request = Request({'type': 'http', 'headers': []})
    if managed:
        if explicit:
            await git_worktrees.reopen_worktree(request, row['id'], wid, token='test')
        else:
            assert project_resolver.resolve_cwd(chat, raise_add_errors=True) == str(topic)
        reopened = database.get_worktree(wid)
        assert reopened == {**completed, 'status': 'active'}
        assert git(topic, 'rev-parse', 'HEAD') == retained
        assert git(topic, 'symbolic-ref', 'HEAD') == 'refs/heads/topic'
    else:
        if explicit:
            with pytest.raises(HTTPException, match='check out the kept branch manually') as error:
                await git_worktrees.reopen_worktree(request, row['id'], wid, token='test')
            assert error.value.status_code == 400
        else:
            with pytest.raises(WorktreeAddError, match='check out the kept branch manually'):
                project_resolver.resolve_cwd(chat, raise_add_errors=True)
        assert database.get_worktree(wid) == completed
        assert not topic.exists()
        assert not (old / '.fluid').exists()
        assert set((old / '.git').rglob('*')) == old_git_paths
    assert database.get_session('branch-chat') == chat
    assert git(old, 'rev-parse', 'topic') == retained
    assert git(new, 'show-ref') == new_refs
    assert git(new, 'worktree', 'list', '--porcelain') == new_listing


@pytest.mark.asyncio
@pytest.mark.parametrize('synced_worktree', ['managed'], indirect=True)
@pytest.mark.parametrize('unavailable', ['clone', 'clone-replaced', 'tip-changed', 'branch-deleted', 'no-owner'])
async def test_reopen_refuses_unproven_retained_checkout(synced_worktree, unavailable):
    from app.core.worktree_manager import WorktreeError, WorktreeManager

    row, old, new, topic, worktree, git = synced_worktree
    wid = worktree['id']
    assert await WorktreeManager().cleanup_worktree(wid, keep_branch=True)
    if unavailable == 'no-owner':
        # Completed pre-upgrade rows never recorded Keep provenance.
        with database.get_db() as conn:
            conn.cursor().execute('UPDATE worktrees SET cleanup_owner = NULL WHERE id = %s', (wid,))
    project_folders.retarget(row, str(new), project_folders.revision(row))
    git(new, 'branch', 'topic')
    new_refs = git(new, 'show-ref')
    new_listing = git(new, 'worktree', 'list', '--porcelain')
    completed = database.get_worktree(wid)
    if unavailable == 'clone':
        old.rename(old.with_name('missing-owner'))
    elif unavailable == 'clone-replaced':
        original = old.rename(old.with_name('moved-owner'))
        git(old.parent, 'clone', '--no-local', str(original), str(old))
        git(old, 'branch', 'topic')
    elif unavailable == 'tip-changed':
        git(old, '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
            'commit', '--allow-empty', '-m', 'advanced after Keep')
        git(old, 'branch', '-f', 'topic', 'HEAD')
    elif unavailable == 'branch-deleted':
        git(old, 'branch', '-D', 'topic')
    original_git_files = set((old / '.git').rglob('*'))
    with pytest.raises(WorktreeError, match='Reopen refused'):
        WorktreeManager().recreate_worktree(wid)
    with pytest.raises(WorktreeError, match='Cleanup refused'):
        await WorktreeManager().cleanup_worktree(wid, keep_branch=False)
    assert set((old / '.git').rglob('*')) == original_git_files
    assert database.get_worktree(wid) == completed
    assert not topic.exists()
    assert git(new, 'show-ref') == new_refs
    assert git(new, 'worktree', 'list', '--porcelain') == new_listing


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
    with pytest.raises(WorktreeError, match='check out the kept branch manually'):
        manager.recreate_worktree(wid)
    assert git(new, 'branch', '--list', 'topic') == 'topic'


@pytest.mark.asyncio
async def test_pinned_cleanup_refuses_active_turn(synced_worktree):
    from app.core.worktree_manager import WorktreeError, WorktreeManager

    row, old, new, topic, worktree, git = synced_worktree
    wid = worktree['id']
    project_folders.retarget(row, str(new), project_folders.revision(row))
    pinned = database.get_worktree(wid)
    database.create_session('busy-worktree', 'default', project_id=row['id'], worktree_id=wid,
                            worktree_branch='topic')
    turn = turns.enqueue_turn('busy-worktree', 'user', prompt='Do work in the old worktree')
    assert turns.start_turn(turn['id'], instance_id=ME)['state'] == 'running'
    for keep in (True, False):
        with pytest.raises(WorktreeError, match='a session is working in this checkout'):
            await WorktreeManager().cleanup_worktree(wid, keep_branch=keep)
    assert database.get_worktree(wid) == pinned
    assert topic.is_dir()
    assert git(old, 'branch', '--list', 'topic') == '+ topic'
    assert git(new, 'branch', '--list', 'topic') == ''


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

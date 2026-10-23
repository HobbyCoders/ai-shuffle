"""A deleted remote branch does not turn landed squash ancestry into new work."""
import subprocess


def test_squash_merge_with_deleted_remote_has_matching_tree(tmp_path):
    from app.core.git_service import GitService
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Offline test')
    git('config', 'user.email', 'test@example.invalid')
    (tmp_path / 'work.txt').write_text('base\n')
    git('add', 'work.txt')
    git('commit', '-m', 'base')
    git('checkout', '-b', 'feature')
    for n in (1, 2):
        (tmp_path / 'work.txt').write_text(f'feature {n}\n')
        git('commit', '-am', f'feature {n}')
    git('checkout', 'main')
    git('merge', '--squash', 'feature')
    git('commit', '-m', 'squash')
    git('checkout', 'feature')
    service = GitService()
    service.invalidate_status_cache(str(tmp_path))
    status = service._get_status_cli(str(tmp_path))
    assert status['ahead'] == 2
    assert status['unpushed'] is None
    assert status['is_clean']
    assert status['matches_base_tree'] is True
    (tmp_path / 'work.txt').write_text('follow-up\n')
    git('commit', '-am', 'follow-up')
    # No invalidation: the base comparison is cached per HEAD commit, so an
    # agent's own commit (which the app never sees) is reflected at once.
    status = service._get_status_cli(str(tmp_path))
    assert status['matches_base_tree'] is False

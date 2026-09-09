"""GitHub auth regressions: scope-limited PATs, persistence, and device flow."""
import asyncio
import io
import os
import shutil
import subprocess
import threading
from unittest.mock import MagicMock

import pytest
import yaml

from app.core import auth as auth_module


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv('GH_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.setenv('GH_CONFIG_DIR', str(tmp_path / 'gh'))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'gitconfig'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setattr(auth_module, 'find_gh_executable', lambda: 'gh')
    return auth_module.AuthService()


def result(code=0, out='', err=''):
    return subprocess.CompletedProcess([], code, stdout=out, stderr=err)


@pytest.mark.parametrize('token', ['ghp_public_only', 'github_pat_fine_grained'])
def test_limited_pat_is_validated_saved_and_read_back(service, monkeypatch, token):
    service.gh_config_dir.mkdir()
    hosts = service.gh_config_dir / 'hosts.yml'
    hosts.write_text(yaml.safe_dump({'enterprise.example': {'oauth_token': 'other'},
        'github.com': {'users': {'other-user': {'oauth_token': 'other-token'}}}}))
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        assert token not in args
        if args[1] == 'api':
            if 'GH_TOKEN' in kw['env']:
                assert kw['env']['GH_TOKEN'] == token
            else:
                saved = yaml.safe_load(hosts.read_text())
                assert saved['github.com']['oauth_token'] == token
                assert saved['github.com']['users']['octocat']['oauth_token'] == token
            return result(out='octocat\n')
        if args[1:3] == ['auth', 'login']:
            assert kw['input'] == token
            return result(1, err="error validating token: missing required scope 'read:org'")
        assert args[1:] == ['auth', 'setup-git', '--hostname', 'github.com']
        return result()

    monkeypatch.setattr(auth_module.subprocess, 'run', run)
    assert service.github_login_with_token('  ' + token + '\n')['success']
    saved = yaml.safe_load(hosts.read_text())
    assert saved['enterprise.example']['oauth_token'] == 'other'
    assert saved['github.com']['users']['other-user']['oauth_token'] == 'other-token'
    assert saved['github.com']['user'] == 'octocat'
    if os.name != 'nt':
        assert hosts.stat().st_mode & 0o777 == 0o600
    assert len(calls) == 4
    assert not list(service.gh_config_dir.glob('.hosts-*'))


def test_classic_pat_uses_native_login_not_fallback(service, monkeypatch):
    run = MagicMock(side_effect=[result(out='octocat'), result(), result(out='octocat'), result()])
    monkeypatch.setattr(auth_module.subprocess, 'run', run)
    fallback = MagicMock()
    monkeypatch.setattr(service, '_save_limited_github_token', fallback)
    assert service.github_login_with_token('classic')['success']
    fallback.assert_not_called()
    assert 'GH_TOKEN' not in run.call_args_list[2].kwargs['env']


@pytest.mark.parametrize('token', ['', ' ', None, 123, 'token\nother'])
def test_empty_or_malformed_pat_never_runs_cli(service, monkeypatch, token):
    run = MagicMock()
    monkeypatch.setattr(auth_module.subprocess, 'run', run)
    assert not service.github_login_with_token(token)['success']
    run.assert_not_called()


def test_invalid_pat_does_not_write_credentials(service, monkeypatch):
    run = MagicMock(return_value=result(1, err='Bad credentials'))
    monkeypatch.setattr(auth_module.subprocess, 'run', run)
    assert not service.github_login_with_token('rejected')['success']
    assert not service.gh_config_dir.exists()
    assert run.call_count == 1


def test_cli_error_redacts_token_and_does_not_fallback(service, monkeypatch):
    monkeypatch.setattr(auth_module.subprocess, 'run', MagicMock(side_effect=[
        result(out='octocat'), result(1, err='cannot store secret-token')]))
    response = service.github_login_with_token('secret-token')
    assert not response['success']
    assert 'secret-token' not in str(response)
    assert not (service.gh_config_dir / 'hosts.yml').exists()


@pytest.mark.parametrize('failure', ['readback', 'git-helper'])
def test_no_success_until_persistence_and_git_helper_work(service, monkeypatch, failure):
    calls = [result(out='octocat'), result(),
             result(out='wrong-account' if failure == 'readback' else 'octocat'), result(1)]
    monkeypatch.setattr(auth_module.subprocess, 'run', MagicMock(side_effect=calls))
    assert not service.github_login_with_token('pat')['success']


def test_expired_hosts_file_is_not_authentication(service, monkeypatch):
    service.gh_config_dir.mkdir()
    (service.gh_config_dir / 'hosts.yml').write_text('github.com: {oauth_token: expired}')
    monkeypatch.setattr(auth_module.subprocess, 'run', MagicMock(return_value=result(1)))
    assert not service.is_github_authenticated()


def test_environment_token_cannot_silently_override_pasted_token(service, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'managed-token')
    run = MagicMock()
    monkeypatch.setattr(auth_module.subprocess, 'run', run)
    assert 'environment' in service.github_login_with_token('pat')['error']
    run.assert_not_called()


def test_limited_pat_file_consumed_by_real_gh_and_git_helper(service):
    """No network or account: exercise producer -> actual installed CLI consumer."""
    gh = shutil.which('gh')
    if not gh:
        pytest.skip('GitHub CLI not installed')
    service.gh_config_dir.mkdir()
    service._save_limited_github_token('local-test-token', 'octocat')
    env = service._github_env()
    token = subprocess.run([gh, 'auth', 'token', '--hostname', 'github.com'],
                           capture_output=True, text=True, env=env, timeout=10)
    assert token.returncode == 0
    assert token.stdout.strip() == 'local-test-token'
    service._setup_github_git(gh)
    credential = subprocess.run(['git', 'credential', 'fill'],
        input='protocol=https\nhost=github.com\n\n', capture_output=True, text=True,
        env={**env, 'GIT_TERMINAL_PROMPT': '0'}, timeout=10)
    assert credential.returncode == 0
    assert 'password=local-test-token' in credential.stdout
    assert 'username=' in credential.stdout


@pytest.mark.asyncio
async def test_web_flow_returns_code_without_blocking_event_loop(service, monkeypatch):
    proc = MagicMock()
    proc.stdout = io.StringIO('! First copy your one-time code: ABCD-1234\n')
    proc.stdin = io.StringIO()
    proc.poll.return_value = None
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(auth_module.subprocess, 'Popen', popen)
    original = asyncio.to_thread
    threads = []
    async def to_thread(fn, *args):
        def record():
            threads.append(threading.get_ident())
            return fn(*args)
        return await original(record)
    monkeypatch.setattr(auth_module.asyncio, 'to_thread', to_thread)
    response = await service.github_login_web()
    assert response['success']
    assert response['user_code'] == 'ABCD-1234'
    assert all(t != threading.get_ident() for t in threads)
    assert popen.call_args.kwargs['stderr'] == subprocess.STDOUT
    assert proc.stdin.getvalue() == '\n'
    assert service.cancel_github_web_login()['success']
    proc.kill.assert_called_once()
    proc.wait.assert_called_once()


@pytest.mark.asyncio
async def test_web_flow_without_code_cleans_up(service, monkeypatch):
    proc = MagicMock(stdout=io.StringIO('network error\n'))
    proc.poll.return_value = None
    monkeypatch.setattr(auth_module.subprocess, 'Popen', MagicMock(return_value=proc))
    assert not (await service.github_login_web())['success']
    assert service._gh_web_login_process is None
    proc.kill.assert_called_once()
    proc.wait.assert_called_once()


@pytest.mark.parametrize('exit_code', [0, 1])
def test_web_completion_reconciles_git_credentials_and_failure(service, monkeypatch, exit_code):
    proc = MagicMock(returncode=exit_code)
    proc.poll.return_value = exit_code
    service._gh_web_login_process = proc
    setup = MagicMock()
    monkeypatch.setattr(service, '_setup_github_git', setup)
    monkeypatch.setattr(service, 'is_github_authenticated', lambda: False)
    info = service.get_github_auth_info()
    assert info['web_login_pending'] is False
    assert bool(info['web_login_error']) == bool(exit_code)
    assert setup.call_count == (1 if exit_code == 0 else 0)

"""Local evidence consistency checks; no PR body or recorded command execution."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location('verification_evidence', Path(__file__).parents[1] / 'scripts' / 'verification_evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    def git(*args):
        subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True)
    git('init')
    (path / 'source.txt').write_text('original')
    git('add', '.')
    git('-c', 'user.name=Evidence test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'fixture')
    return path


def command(xml=None, exit_code=0, extra=''):
    xml = xml or '<testsuite tests="2" failures="0" errors="0" skipped="1"><testcase name="pass"/><testcase name="skip"><skipped/></testcase></testsuite>'
    code = ("from pathlib import Path; import sys; "
            f"Path(sys.argv[1]).write_text({xml!r}); "
            "print('WARNING: retained output'); " + extra + f"sys.exit({exit_code})")
    return [sys.executable, '-c', code, '{junit}']


def test_resolves_command_launcher_before_execution(repo, tmp_path, monkeypatch):
    output = tmp_path / 'resolved-launcher'
    args = command()
    args[0] = 'round8-python-launcher'
    monkeypatch.setattr(evidence.shutil, 'which', lambda name: sys.executable)
    result = evidence.record(repo, output, args)
    assert result['command'][0] == sys.executable
    assert evidence.check(repo, output)['counts']['passed'] == 1


def test_producer_records_command_revision_output_and_counts(repo, tmp_path):
    output = tmp_path / 'evidence'
    result = evidence.record(repo, output, command())
    assert result['exit_code'] == 0
    assert result['before'] == result['after'] == evidence.snapshot(repo)
    assert result['command'][-1] == str(output / 'junit.xml')
    assert 'WARNING' in (output / 'output.log').read_text()
    assert evidence.check(repo, output, {'tests': 2, 'passed': 1, 'skipped': 1}) == result
    with pytest.raises(ValueError, match='incorrect count'):
        evidence.check(repo, output, {'tests': 86})
    with pytest.raises(ValueError, match='Unsupported'):
        evidence.check(repo, output, {'warnings': 0})


@pytest.mark.parametrize('kind', ['tracked', 'untracked', 'revision', 'during'])
def test_rejects_changed_worktree(repo, tmp_path, kind):
    output = tmp_path / 'evidence'
    extra = "Path('source.txt').write_text('during'); " if kind == 'during' else ''
    evidence.record(repo, output, command(extra=extra))
    if kind == 'tracked':
        (repo / 'source.txt').write_text('changed')
    elif kind == 'untracked':
        (repo / 'new.py').write_text('new')
    elif kind == 'revision':
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=t@example.invalid', 'commit', '--allow-empty', '-m', 'new'], check=True, capture_output=True)
    with pytest.raises(ValueError, match='Worktree changed'):
        evidence.check(repo, output)


@pytest.mark.parametrize('artifact', ['output.log', 'junit.xml'])
def test_rejects_altered_artifacts(repo, tmp_path, artifact):
    output = tmp_path / 'evidence'
    evidence.record(repo, output, command())
    (output / artifact).write_text('replaced')
    with pytest.raises(ValueError, match='Artifact changed'):
        evidence.check(repo, output)


@pytest.mark.parametrize('exit_code,timeout,extra', [(7, 10, ''), (0, .05, 'import time; time.sleep(2); ')])
def test_rejects_failed_and_timed_out_runs(repo, tmp_path, exit_code, timeout, extra):
    output = tmp_path / 'evidence'
    result = evidence.record(repo, output, command(exit_code=exit_code, extra=extra), timeout)
    assert result['exit_code'] != 0
    with pytest.raises(ValueError, match='failed or timed out'):
        evidence.check(repo, output)


@pytest.mark.parametrize('xml', [
    '<testsuite tests="86"><testcase/></testsuite>',
    '<testsuite tests="0"/>',
    '<testsuite tests="1" failures="1"><testcase><failure/></testcase></testsuite>',
])
def test_rejects_inconsistent_empty_or_failing_junit(repo, tmp_path, xml):
    output = tmp_path / 'evidence'
    evidence.record(repo, output, command(xml=xml))
    with pytest.raises(ValueError):
        evidence.check(repo, output)


def test_checker_never_executes_command_from_manifest(repo, tmp_path):
    output = tmp_path / 'evidence'
    evidence.record(repo, output, command())
    manifest = output / 'evidence.json'
    result = json.loads(manifest.read_text())
    marker = tmp_path / 'must-not-exist'
    result['command'] = [sys.executable, '-c', f"open({str(marker)!r}, 'w').close()"]
    manifest.write_text(json.dumps(result))
    # The local manifest is unsigned. Checking does not grant its command authority.
    evidence.check(repo, output)
    assert not marker.exists()


def test_missing_junit_cannot_be_claimed_as_completed_tests(repo, tmp_path):
    output = tmp_path / 'evidence'
    evidence.record(repo, output, [sys.executable, '-c', "print('launched elsewhere')"])
    with pytest.raises(OSError):
        evidence.check(repo, output)


def test_command_runs_in_requested_subdirectory(repo, tmp_path):
    package = repo / 'package'
    package.mkdir()
    output = tmp_path / 'evidence'
    result = evidence.record(package, output, command(extra="assert Path.cwd().name == 'package'; "))
    assert result['cwd'] == str(package)
    evidence.check(repo, output)


def test_cli_rejects_bad_claim_before_following_write(repo, tmp_path):
    output = tmp_path / 'evidence'
    script = str(Path(evidence.__file__))
    run = subprocess.run([sys.executable, script, '--cwd', str(repo), 'run',
                          '--output', str(output), '--', *command()], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    marker = tmp_path / 'write-must-not-run'
    check = [sys.executable, script, '--cwd', str(repo), 'check', str(output), '--tests', '86']
    gate = 'import subprocess; from pathlib import Path; ' + f'subprocess.run({check!r}, check=True); Path({str(marker)!r}).touch()'
    failed = subprocess.run([sys.executable, '-c', gate], capture_output=True, text=True)
    assert failed.returncode != 0
    assert 'incorrect count' in failed.stdout
    assert not marker.exists()




def test_timeout_stops_real_child_process_tree(repo, tmp_path):
    psutil = pytest.importorskip('psutil')
    marker = tmp_path / 'child.pid'
    program = (
        'import subprocess,sys,time; from pathlib import Path; '
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
        f'Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)'
    )
    result = evidence.record(repo, tmp_path / 'tree-evidence',
                             [sys.executable, '-c', program], timeout=2)
    assert result['timed_out'] and result['exit_code'] == 124
    pid = int(marker.read_text())
    try:
        assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass


def test_rejects_worktree_artifacts_and_reusing_run_directory(repo, tmp_path):
    with pytest.raises(ValueError, match='outside'):
        evidence.record(repo, repo / 'evidence', command())
    output = tmp_path / 'evidence'
    evidence.record(repo, output, command())
    with pytest.raises(FileExistsError):
        evidence.record(repo, output, command())

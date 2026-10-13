"""Record/check local JUnit test evidence, not arbitrary PR prose or live behavior.

Run: python scripts/verification_evidence.py run --output /outside/repo/run-1 -- pytest --junitxml={junit}
Check: python scripts/verification_evidence.py check /outside/repo/run-1 --tests 12 --passed 12

The checker never executes a recorded command. It detects changes to the files
the change touches, failed/timed-out runs, artifact changes and inconsistent
JUnit counts. Evidence is unsigned local data, not an attestation against a
malicious author. Ignored files, dependencies, external services and browser
observations are out of scope. No warning-count or general PR-claim
verification is provided. Use `check && gh ...` for an explicit local write
gate; direct gh usage is not intercepted.

Scope of the staleness check (round-9 change). The hash covers the paths the
change touches — every path that differs from the base ref (``--base``,
default origin/main, then main), every uncommitted tracked change, and every
untracked non-ignored file — as they stood when the run was recorded. It does
NOT cover the rest of the worktree: sibling agents editing other files in a
shared worktree used to trip "Worktree changed during or after verification"
on runs that were perfectly good, and the model re-ran whole suites. A file the
verification command reads but the change does not touch is therefore out of
scope; that is deliberate, since determining a command's read set is not cheap.
A rejection names the files that changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(cwd, *args):
    return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.PIPE)


DEFAULT_BASES = ('origin/main', 'main')


def _resolve_base(root, base):
    """The base ref to diff against, or None when none of the candidates exist."""
    candidates = (base,) if base else DEFAULT_BASES
    for candidate in candidates:
        try:
            git(root, 'rev-parse', '--verify', '--quiet', candidate + '^{commit}')
            return candidate
        except subprocess.CalledProcessError:
            continue
    return None


def touched_paths(root, base=None):
    """The paths this change touches, relative to ``root``, sorted.

    Committed changes since the merge-base with ``base`` (when a base ref
    resolves), uncommitted tracked changes, and untracked non-ignored files.
    """
    names = set()
    resolved = _resolve_base(root, base)
    if resolved:
        try:
            merge_base = git(root, 'merge-base', resolved, 'HEAD').decode().strip()
            names.update(git(root, 'diff', '--name-only', '-z', merge_base, 'HEAD').decode('utf-8').split('\0'))
        except subprocess.CalledProcessError:
            pass
    names.update(git(root, 'diff', '--name-only', '-z', 'HEAD').decode('utf-8').split('\0'))
    names.update(git(root, 'ls-files', '--others', '--exclude-standard', '-z').decode('utf-8').split('\0'))
    return sorted(name for name in names if name), resolved


def _hash_paths(root, names):
    """``{path: sha256 | '<missing>'}`` for the working-tree content of ``names``."""
    out = {}
    for name in names:
        path = root / name
        out[name] = digest(path.read_bytes()) if path.is_file() else '<missing>'
    return out


def snapshot(cwd, base=None, paths=None):
    """The state of the touched files. ``paths`` re-hashes a recorded set."""
    root = Path(git(cwd, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    revision = git(root, 'rev-parse', 'HEAD').decode().strip()
    if paths is None:
        paths, base = touched_paths(root, base)
    return {'worktree': str(root), 'revision': revision, 'base': base,
            'paths': _hash_paths(root, paths)}


def changed_paths(before, after):
    """Paths whose recorded hash differs between two snapshots of one set."""
    names = set(before.get('paths', {})) | set(after.get('paths', {}))
    return sorted(name for name in names
                  if before.get('paths', {}).get(name) != after.get('paths', {}).get(name))


def junit_counts(path):
    root = ET.parse(path).getroot()
    if root.tag not in ('testsuite', 'testsuites'):
        raise ValueError('Expected JUnit testsuite(s)')
    cases = list(root.iter('testcase'))
    counts = {'tests': len(cases), 'passed': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    for case in cases:
        outcomes = [name for name in ('failure', 'error', 'skipped') if case.find(name) is not None]
        if len(outcomes) > 1:
            raise ValueError('Conflicting JUnit outcomes')
        key = {'failure': 'failures', 'error': 'errors', 'skipped': 'skipped'}.get(outcomes[0] if outcomes else '', 'passed')
        counts[key] += 1
    # Validate declared totals as well as counting leaf cases (no suite double count).
    for suite in root.iter():
        if suite.tag not in ('testsuite', 'testsuites'):
            continue
        descendants = list(suite.iter('testcase'))
        for field, tag in (('tests', None), ('failures', 'failure'), ('errors', 'error'), ('skipped', 'skipped')):
            actual = len(descendants) if tag is None else sum(c.find(tag) is not None for c in descendants)
            if field in suite.attrib and int(suite.attrib[field]) != actual:
                raise ValueError(f'Inconsistent JUnit {field}')
    return counts


def _terminate_tree(process):
    """Stop descendants before reaping their parent on timeout/interruption."""
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def record(cwd, output, command, timeout=600, base=None):
    cwd = Path(cwd).resolve()
    before = snapshot(cwd, base)
    output = Path(output).resolve()
    if output.is_relative_to(Path(before['worktree'])):
        raise ValueError('Evidence must be outside the worktree')
    if not command or timeout <= 0 or timeout > 1200:
        raise ValueError('Supply a command and a timeout in (0, 1200]')
    output.mkdir(parents=True, exist_ok=False)
    command = [arg.replace('{junit}', str(output / 'junit.xml')) for arg in command]
    # CreateProcess does not expand PATHEXT: resolve npm to npm.cmd on Windows.
    command[0] = shutil.which(command[0]) or command[0]
    started = time.time()
    timed_out = False
    with (output / 'output.log').open('wb') as log:
        try:
            options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                       if os.name == 'nt' else {'start_new_session': True})
            process = subprocess.Popen(command, cwd=cwd, stdout=log,
                                       stderr=subprocess.STDOUT, **options)
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_tree(process)
                timed_out, exit_code = True, 124
            except BaseException:
                _terminate_tree(process)
                raise
        except OSError as exc:
            log.write(str(exc).encode('utf-8'))
            exit_code = 127
    after = snapshot(cwd, before['base'], paths=list(before['paths']))
    counts, error = None, None
    try:
        counts = junit_counts(output / 'junit.xml')
    except (OSError, ValueError, ET.ParseError) as exc:
        error = str(exc)
    artifacts = {name: digest((output / name).read_bytes()) for name in ('output.log', 'junit.xml') if (output / name).is_file()}
    evidence = dict(version=2, scope='JUnit test execution only', command=command, cwd=str(cwd),
                    started_at=started, finished_at=time.time(), timeout_seconds=timeout,
                    exit_code=exit_code, timed_out=timed_out, before=before, after=after,
                    counts=counts, junit_error=error, artifacts=artifacts)
    (output / 'evidence.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    return evidence


def check(cwd, output, claims=None):
    output = Path(output)
    evidence = json.loads((output / 'evidence.json').read_text(encoding='utf-8'))
    if evidence.get('version') != 2 or evidence.get('scope') != 'JUnit test execution only':
        raise ValueError('Unsupported evidence format/scope')
    if evidence.get('exit_code') != 0 or evidence.get('timed_out') is not False:
        raise ValueError('Command failed or timed out')
    before, after = evidence['before'], evidence['after']
    if set(before.get('paths', {})) != set(after.get('paths', {})):
        raise ValueError('Evidence is inconsistent: touched path set differs between snapshots')
    during = changed_paths(before, after)
    if during:
        raise ValueError('Touched files changed during verification: ' + ', '.join(during))
    now = snapshot(cwd, after.get('base'), paths=list(after['paths']))
    if now['worktree'] != after.get('worktree'):
        raise ValueError('Evidence was recorded in a different worktree')
    since = changed_paths(after, now)
    if since:
        raise ValueError('Touched files changed after verification: ' + ', '.join(since))
    for name in ('output.log', 'junit.xml'):
        if digest((output / name).read_bytes()) != evidence['artifacts'].get(name):
            raise ValueError(f'Artifact changed: {name}')
    counts = junit_counts(output / 'junit.xml')
    if evidence.get('junit_error') or counts != evidence.get('counts'):
        raise ValueError('JUnit evidence is inconsistent')
    if not counts['tests'] or counts['failures'] or counts['errors']:
        raise ValueError('JUnit has no tests or has failures/errors')
    for name, value in (claims or {}).items():
        if name not in counts or counts[name] != value:
            raise ValueError(f'Unsupported or incorrect count: {name}={value}')
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd', default='.')
    commands = parser.add_subparsers(dest='action', required=True)
    run = commands.add_parser('run')
    run.add_argument('--output', required=True)
    run.add_argument('--timeout', type=float, default=600)
    run.add_argument('--base', help='Base ref the change is measured against (default: origin/main, then main).')
    run.add_argument('command', nargs=argparse.REMAINDER)
    verify = commands.add_parser('check')
    verify.add_argument('output')
    for key in ('tests', 'passed', 'skipped'):
        verify.add_argument('--' + key, type=int)
    args = parser.parse_args()
    try:
        if args.action == 'run':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            evidence = record(args.cwd, args.output, command, args.timeout, base=args.base)
            print(str(Path(args.output).resolve() / 'evidence.json'))
            check(args.cwd, args.output)
        else:
            claims = {key: getattr(args, key) for key in ('tests', 'passed', 'skipped') if getattr(args, key) is not None}
            evidence = check(args.cwd, args.output, claims)
        touched = sorted(evidence.get('after', {}).get('paths', {}))
        summary = {'scope': evidence['scope'], 'counts': evidence['counts'], 'touched_paths': touched}
        if not touched:
            # Nothing touched: the run verified the base itself, which is fine,
            # but say so rather than implying a change was covered.
            summary['note'] = 'No touched paths relative to the base; the run covered an unchanged tree.'
        print(json.dumps(summary))
        return 0
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError) as exc:
        print(f'Evidence rejected: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
import { test } from 'node:test';
import { setTimeout as sleep } from 'node:timers/promises';
import { startClaudeAuthWorker } from './claude-auth-worker.mjs';

test('early exit and readiness timeout report safely, dispose, and permit a fresh launch', { timeout: 8_000 }, async (t) => {
	const temp = mkdtempSync(join(tmpdir(), 'claude-worker-test-'));
	t.after(() => rmSync(temp, { recursive: true, force: true }));
	const launcher = join(temp, 'launcher');
	const env = { ...process.env };
	t.after(() => {
		for (const key of ['DEVLOOP_PYTHON', 'TMPDIR', 'TEST_LAUNCH']) {
			if (env[key] === undefined) delete process.env[key];
			else process.env[key] = env[key];
		}
	});
	process.env.TMPDIR = temp;
	process.env.DEVLOOP_PYTHON = launcher;
	writeFileSync(launcher, String.raw`#!/usr/bin/env node
const { mkdtempSync, rmSync, writeFileSync } = require('node:fs');
const { join } = require('node:path');
const status = process.argv.at(-1);
const scratch_dir = mkdtempSync(join(process.env.TMPDIR, 'worker-'));
const cleanup = () => { rmSync(status); rmSync(scratch_dir, { recursive: true }); };
process.on('SIGTERM', () => { cleanup(); process.exit(0); });
writeFileSync(status, JSON.stringify({ pid: process.pid, scratch_dir, claude_cli: {mode: 'fake'}, app: {local_port: 1234} }));
console.log('[devloop] status file: fixture-value-must-not-appear');
if (process.env.TEST_LAUNCH === 'exit') {
	const noise = 'fixture-value-must-not-appear'.repeat(4096);
	process.stderr.write(noise + '\n');
	process.stderr.write(('Traceback (most recent call last)\nRuntimeFailure: managed PostgreSQL exited unexpectedly\n').repeat(30));
	cleanup();
	process.exitCode = 23;
} else {
	if (process.env.TEST_LAUNCH === 'ready') console.log('DEVLOOP_READY {}');
	setInterval(() => {}, 1000);
}
`, { mode: 0o700 });

	for (const stage of ['exit', 'timeout', 'ready']) {
		process.env.TEST_LAUNCH = stage;
		let cleanup;
		const messages = [];
		try {
			const startup = startClaudeAuthWorker({ deferCleanup: (fn) => { cleanup = fn; }, log: (line) => messages.push(line) },
				{ readyMs: 1_000 });
			if (stage === 'ready') {
				const worker = await startup;
				assert.ok(worker.pid > 0);
			} else {
				await assert.rejects(startup, (error) => {
					if (stage === 'exit') {
						assert.match(error.message, /exited before ready \(code 23\)/);
						assert.match(error.message, /stderr: PostgreSQL exited during startup/);
					} else {
						assert.match(error.message, /did not become ready/);
						assert.match(error.message, /stdout: private status published/);
					}
					assert.doesNotMatch(error.message, /fixture-value-must-not-appear/);
					assert.ok(error.message.split('\n').length <= 10);
					return true;
				});
			}
		} finally {
			await cleanup();
		}
		assert.match(messages.at(-1), /disposed fake Claude worker/);
		assert.deepEqual(readdirSync(temp), ['launcher']);
	}
});

function running(pid, start) {
	try {
		const stat = readFileSync(`/proc/${pid}/stat`, 'utf8');
		const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
		return fields[0] !== 'Z' && fields[19] === start;
	} catch (error) {
		if (error.code !== 'ENOENT' && error.code !== 'ESRCH') throw error;
		return false;
	}
}

async function waitUntil(done, ms) {
	const stop = performance.now() + ms;
	while (!done()) {
		assert.ok(performance.now() < stop, 'fixture did not reach its expected process state');
		await sleep(20);
	}
}

for (const stage of ['no-guardian', 'pre-ready', 'published', 'clean']) {
	test(stage === 'clean' ? 'clean exit with a ready guardian' : `forced stop with ${stage} guardian`,
		{ timeout: 20_000 }, async (t) => {
		const temp = mkdtempSync(join(tmpdir(), 'claude-worker-kill-test-'));
		const launcher = join(temp, 'launcher');
		const old = { TMPDIR: process.env.TMPDIR, DEVLOOP_PYTHON: process.env.DEVLOOP_PYTHON };
		t.after(() => {
			for (const [key, value] of Object.entries(old)) {
				if (value === undefined) delete process.env[key];
				else process.env[key] = value;
			}
			rmSync(temp, { recursive: true, force: true });
		});
		process.env.TMPDIR = temp;
		process.env.DEVLOOP_PYTHON = launcher;
		const unrelated = join(temp, 'not-owned-by-the-fixture');
		writeFileSync(unrelated, 'keep me');
		writeFileSync(launcher, String.raw`#!/usr/bin/env python3
import json, os, signal, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.getcwd())
from scripts.devloop.guardian import identity, spawn_guardian, SCRATCH_MARKER, SCRATCH_PREFIX

status = Path(sys.argv[-1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pid = os.getpid()
start = identity(pid)
stage = '${stage}'
guardian = scratch = None
if stage != 'no-guardian':
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    if stage != 'published':
        (scratch / SCRATCH_MARKER).write_text(json.dumps(dict(pid=pid, start=start, uid=os.getuid())))
        (scratch / 'do-not-copy-private-text').write_text('private test payload')
    lease = {}
    if '--guardian-lease-fd' in sys.argv:  # The regression can also run on the old launcher contract.
        lease_fd = int(sys.argv[sys.argv.index('--guardian-lease-fd') + 1])
        os.set_inheritable(lease_fd, False)
        lease['lease_fd'] = lease_fd
    guardian = spawn_guardian(status, scratch, **lease)
    assert guardian and guardian['pid_identity']
    if stage != 'pre-ready':
        status.write_text(json.dumps(dict(pid=pid, pid_identity=start, scratch_dir=str(scratch),
            guardian=guardian, claude_cli={'mode': 'fake'}, app={'local_port': 1234})))
        print('[devloop] status file: do-not-copy-private-text')
    if stage == 'clean':
        def stop(*_):
            import shutil
            status.unlink()
            shutil.rmtree(scratch)
            sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
Path(os.environ['TMPDIR'], 'fixture.json').write_text(json.dumps(dict(
    pid=pid, start=start, guardian=guardian, scratch=str(scratch) if scratch else None, status=str(status))))
print('DEVLOOP_READY {}' if stage == 'clean' else 'DEVLOOP_FAILED do-not-copy-private-text', flush=True)
while True:
    signal.pause()
`, { mode: 0o700 });
		let cleanup, fixture, pausedGuardian = false, retainedWhileGuardianWasLive;
		const messages = [];
		try {
			const startup = startClaudeAuthWorker({
				deferCleanup: (fn) => { cleanup = fn; }, log: (line) => messages.push(line)
			});
			if (stage === 'clean') {
				assert.ok((await startup).pid > 0);
			} else {
				await assert.rejects(startup, (error) => {
					assert.match(error.message, /failed to start\nStartup log \(redacted\):[\s\S]*devloop reported a readiness failure/);
					assert.doesNotMatch(error.message, /do-not-copy-private-text/);
					return true;
				});
			}
			fixture = JSON.parse(readFileSync(join(temp, 'fixture.json'), 'utf8'));
			if (stage === 'pre-ready') {
				// Freeze the actual detached guardian so the fixture cannot win by luck.
				// There is no status for the harness to read, even after the guardian forked.
				assert.equal(existsSync(fixture.status), false);
				assert.equal(existsSync(fixture.scratch), true);
				process.kill(fixture.guardian.pid, 'SIGSTOP');
				pausedGuardian = true;
			}
		} finally {
			const stopped = fixture && stage !== 'clean'
				? assert.rejects(cleanup(), { message: /^Disposable Claude worker did not stop after SIGTERM/ })
				: cleanup();
			if (pausedGuardian) {
				try {
					await waitUntil(() => !running(fixture.pid, fixture.start), 14_000);
					await Promise.race([stopped, sleep(150)]);
					retainedWhileGuardianWasLive = existsSync(dirname(fixture.status));
				} finally {
					process.kill(fixture.guardian.pid, 'SIGCONT');
				}
			}
			await stopped;
		}
		assert.throws(() => process.kill(fixture.pid, 0), { code: 'ESRCH' });
		if (fixture.guardian) await waitUntil(() => !running(fixture.guardian.pid, fixture.guardian.pid_identity), 2_000);
		if (stage === 'pre-ready') {
			assert.equal(existsSync(fixture.scratch), false, 'the detached guardian must remove its owned scratch');
			assert.match(messages.join('\n'), /devloop guardian.*removed owned scratch/, 'guardian cleanup evidence was lost');
			assert.equal(retainedWhileGuardianWasLive, true, 'status directory disappeared while the guardian still owned it');
		}
		if (stage === 'published') {
			assert.match(messages.join('\n'), /devloop guardian.*removed stale status/);
			assert.ok(messages.some((line) => line.startsWith('additional fixture cleanup failure:')));
			assert.equal(existsSync(fixture.scratch), true, 'do not remove scratch without ownership');
		}
		if (stage === 'clean') assert.doesNotMatch(messages.join('\n'), /guardian handled interrupted teardown/);
		assert.equal(readFileSync(unrelated, 'utf8'), 'keep me');
		assert.deepEqual(readdirSync(temp).sort(), [
			'fixture.json', 'launcher', 'not-owned-by-the-fixture', ...(stage === 'published' ? [basename(fixture.scratch)] : [])
		].sort());
		assert.doesNotMatch(messages.join('\n'), /not-owned-by-the-fixture|do-not-copy-private-text|ai-shuffle-preview-/);
	});
}

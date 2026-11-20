import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
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

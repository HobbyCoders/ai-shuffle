// One fake Claude worker per case. A browser context cannot isolate the
// subscription pin (database), the saved logins, or the Claude CLI home.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const checkout = fileURLToPath(new URL('../../../../', import.meta.url));

function deadline(promise, ms, message) {
	let timer;
	return Promise.race([promise, new Promise((_, reject) => {
		timer = setTimeout(() => reject(new Error(message)), ms);
	})]).finally(() => clearTimeout(timer));
}

// Do not forward child text (including DEVLOOP_FAILED JSON). Bootstrap can
// print preview links, SQL or environment values. Retain only fixed labels for
// recognizable milestones/failures; unknown output is intentionally redacted.
const startupEvents = [
	[/^\[devloop\] status file:/, 'private status published'],
	[/^\[devloop\] app starting \(pid /, 'app starting'],
	[/^DEVLOOP_FAILED /, 'devloop reported a readiness failure'],
	[/RuntimeFailure: managed PostgreSQL exited (?:before authenticated readiness|unexpectedly)/, 'PostgreSQL exited during startup'],
	[/RuntimeFailure: (?:initdb|postgres|pg_ctl) failed;/, 'PostgreSQL command failed'],
	[/RuntimeFailure: authenticated database readiness timed out or was interrupted/, 'PostgreSQL readiness timed out or was interrupted'],
	[/RuntimeFailure: managed PostgreSQL (?:server|initdb) major must be 16/, 'PostgreSQL 16 required'],
	[/Error: .*['"](?:postgres|initdb|pg_ctl)['"]/, 'PostgreSQL command failed'],
	[/error: .*serves the stamped build/, 'stamped frontend build unavailable'],
	[/Traceback \(most recent call last\)/, 'startup traceback (details redacted)'],
];

function startupLines(stream, onLine) {
	let prefix = '';
	stream.setEncoding('utf8');
	stream.on('data', (chunk) => {
		const lines = chunk.split('\n');
		for (let i = 0; i < lines.length; i++) {
			prefix += lines[i].slice(0, 512 - prefix.length);
			if (i < lines.length - 1) {
				onLine(prefix);
				prefix = '';
			}
		}
	});
	stream.on('end', () => { if (prefix) onLine(prefix); });
}

function readStatus(path) {
	try {
		return JSON.parse(readFileSync(path, 'utf8'));
	} catch {
		throw new Error('Disposable Claude worker has missing or unreadable private status');
	}
}

export async function startClaudeAuthWorker({ deferCleanup, log }, { readyMs = 31_000 } = {}) {
	const directory = mkdtempSync(join(tmpdir(), 'claude-auth-case-'));
	const statusPath = join(directory, 'status.json');
	const python = process.env.DEVLOOP_PYTHON || 'python';
	const child = spawn(python, ['-m', 'scripts.devloop', 'up', '--ci', '--claude-cli', 'fake',
		'--no-pause', '--ttl-seconds', '600', '--status', statusPath],
	{ cwd: checkout, stdio: ['ignore', 'pipe', 'pipe'] });
	const exited = new Promise((resolve) => child.once('close', resolve));
	let scratch;
	deferCleanup(async () => {
		try {
			if (!scratch && existsSync(statusPath)) scratch = readStatus(statusPath).scratch_dir;
		} finally {
			if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
			try {
				await deadline(exited, 12_000, 'Disposable Claude worker did not stop after SIGTERM');
			} catch (error) {
				child.kill('SIGKILL'); // The devloop guardian reaps orphaned services.
				child.stdout.destroy();
				child.stderr.destroy();
				await deadline(exited, 1_000, 'Disposable Claude worker did not exit after SIGKILL');
				throw error;
			}
		}
		if (existsSync(statusPath) || (scratch && existsSync(scratch))) {
			throw new Error('Disposable Claude worker left state on disk');
		}
		rmSync(directory, { recursive: true }); // only our unique status directory
		log(`disposed fake Claude worker ${child.pid ?? '(not started)'} and its auth state`);
	});

	const events = [];
	let starting = true;
	function note(stream, line) {
		if (!starting) return;
		const label = startupEvents.find(([pattern]) => pattern.test(line))?.[1];
		if (!label) return;
		const entry = `${stream}: ${label}`;
		if (events.at(-1) !== entry) events.push(entry);
		if (events.length > 8) events.shift();
	}
	try {
		await deadline(new Promise((resolve, reject) => {
			child.once('error', () => reject(new Error('Could not spawn disposable Claude worker')));
			child.once('close', (code, signal) => reject(new Error(
				`Disposable Claude worker exited before ready (${signal || `code ${code}`})`)));
			startupLines(child.stderr, (line) => note('stderr', line));
			startupLines(child.stdout, (line) => {
				note('stdout', line);
				if (line.startsWith('DEVLOOP_READY ')) resolve();
				if (line.startsWith('DEVLOOP_FAILED ')) reject(new Error('Disposable Claude worker failed to start'));
			});
		}), readyMs, 'Disposable Claude worker did not become ready');
		const worker = readStatus(statusPath);
		scratch = worker.scratch_dir;
		if (worker.pid !== child.pid || worker.claude_cli?.mode !== 'fake' || !worker.app?.local_port) {
			throw new Error('Expected the private fake Claude worker started for this case');
		}
		log(`started disposable fake Claude worker ${child.pid}`);
		return worker;
	} catch (error) {
		const detail = events.length ? events.join('\n') : '(no recognized events; raw output redacted)';
		throw new Error(`${error.message}\nStartup log (redacted):\n${detail}`);
	} finally {
		starting = false;
	}
}

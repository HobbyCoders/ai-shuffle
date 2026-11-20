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

function readCleanupStatus(path) {
	try {
		return JSON.parse(readFileSync(path, 'utf8'));
	} catch (error) {
		if (error.code !== 'ENOENT') throw new Error('Disposable Claude worker left unreadable private status');
	}
}

function readGuardianReport(statusPath) {
	let report;
	try {
		report = readFileSync(`${statusPath}.guardian.log`, 'utf8');
	} catch (error) {
		if (error.code === 'ENOENT') return [];
		throw new Error('Could not read the disposable Claude worker guardian diagnostic');
	}
	// Log only action labels, not the guardian's paths, PIDs or raw text.
	const actions = [...report.matchAll(/(?:died before its teardown: |; )(killed (?:app|vite|postgres)|removed stale status|removed private PostgreSQL socket directory|removed owned scratch|failed to remove owned scratch) /g)];
	if (!actions.length) throw new Error('Unrecognized disposable Claude worker guardian diagnostic');
	return [...new Set(actions.map(([, action]) => action))];
}

export async function startClaudeAuthWorker({ deferCleanup, log }, { readyMs = 31_000 } = {}) {
	const directory = mkdtempSync(join(tmpdir(), 'claude-auth-case-'));
	const statusPath = join(directory, 'status.json');
	const python = process.env.DEVLOOP_PYTHON || 'python';
	const child = spawn(python, ['-m', 'scripts.devloop', 'up', '--ci', '--claude-cli', 'fake',
		'--no-pause', '--ttl-seconds', '600', '--guardian-lease-fd', '3', '--status', statusPath],
	{ cwd: checkout, stdio: ['ignore', 'pipe', 'pipe', 'pipe'] });
	// ChildProcess 'close' also waits for fd 3, which can outlive the launcher.
	// Reap the launcher and drain its output separately from the ownership lease.
	const lease = child.stdio[3];
	const released = new Promise((resolve) => {
		lease.once('end', () => resolve(true));
		lease.once('close', () => resolve(false));
		lease.once('error', () => resolve(false));
	});
	lease.resume();
	const exited = Promise.all([
		new Promise((resolve) => {
			child.once('exit', (code, signal) => resolve({ code, signal }));
			child.on('error', () => { if (!child.pid) resolve(null); });
		}),
		...[child.stdout, child.stderr].map((stream) => new Promise((resolve) => stream.once('close', resolve))),
	]).then(([exit]) => exit);
	let status;
	deferCleanup(async () => {
		// Remember published scratch for the final check, not as a source of
		// guardian ownership. Never let a status read prevent sending TERM.
		const rememberStatus = () => {
			try { status = readCleanupStatus(statusPath) ?? status; } catch { /* checked again after close */ }
		};
		rememberStatus();
		let stopError;
		if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
		try {
			await deadline(exited, 12_000, 'Disposable Claude worker did not stop after SIGTERM');
		} catch (error) {
			stopError = error;
			rememberStatus();
			child.kill('SIGKILL'); // The devloop guardian reaps orphaned services.
			child.stdout.destroy();
			child.stderr.destroy();
		}
		try {
			const exit = stopError
				? await deadline(exited, 1_000, 'Disposable Claude worker did not exit after SIGKILL')
				: await exited;
			// EOF proves both the launcher and any guardian (even pre-status)
			// have closed their copies. On timeout or I/O error leave the directory alone.
			if (exit && !await deadline(released, 11_500, 'Disposable Claude worker guardian did not finish')) {
				throw new Error('Could not verify release of the disposable Claude worker guardian lease');
			}
			const actions = readGuardianReport(statusPath);
			if (actions.length) log(`devloop guardian handled interrupted teardown: ${actions.join('; ')}`);
			status = readCleanupStatus(statusPath) ?? status;
			const stateLeft = actions.includes('failed to remove owned scratch') || existsSync(statusPath)
				|| (status?.scratch_dir && existsSync(status.scratch_dir));
			try {
				rmSync(directory, { recursive: true }); // only our unique status directory
			} catch {
				throw new Error('Could not remove disposable Claude worker private status directory');
			}
			if (stateLeft) throw new Error('Disposable Claude worker left state on disk after shutdown');
			log(`disposed fake Claude worker ${child.pid ?? '(not started)'} private status directory`);
		} catch (error) {
			if (!stopError) throw error;
			log(`additional fixture cleanup failure: ${error.message}`);
		} finally {
			lease.destroy();
		}
		if (stopError) throw stopError;
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
			exited.then((exit) => reject(new Error(exit
				? `Disposable Claude worker exited before ready (${exit.signal || `code ${exit.code}`})`
				: 'Could not spawn disposable Claude worker')));
			startupLines(child.stderr, (line) => note('stderr', line));
			startupLines(child.stdout, (line) => {
				note('stdout', line);
				if (line.startsWith('DEVLOOP_READY ')) resolve();
				if (line.startsWith('DEVLOOP_FAILED ')) reject(new Error('Disposable Claude worker failed to start'));
			});
		}), readyMs, 'Disposable Claude worker did not become ready');
		const worker = readStatus(statusPath);
		status = worker;
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

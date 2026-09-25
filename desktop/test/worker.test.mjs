import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import http from 'node:http';
import { tmpdir } from 'node:os';
import net from 'node:net';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { resolveRuntime, buildWorkerEnv, isPortFree, fetchHealth, HEALTH_TOTAL_TIMEOUT_MS, attachTokenHash, readWorkerProcessRecord, requestBrowserHandoff, WorkerSupervisor, ATTACH_ANY_ENV, DEFAULT_PORT, PROTECTED_PORT } = require('../dist/main/worker.js');
const { writePrivateFile } = require('../dist/main/private-file.js');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const postgres = { host: 'db', port: 5432, user: 'u', password: 'pw', database: 'd' };

async function fakeBundle(root, platform) {
	const python = platform === 'win32' ? path.join(root, 'python', 'python.exe') : path.join(root, 'python', 'bin', 'python3');
	await mkdir(path.dirname(python), { recursive: true });
	await writeFile(python, '');
	await mkdir(path.join(root, 'server', 'app'), { recursive: true });
	await writeFile(path.join(root, 'server', 'app', 'main.py'), '');
	await writeFile(path.join(root, 'manifest.json'), JSON.stringify({ stub: false, python: { version: '3.12.14' } }));
	return python;
}

test('resolveRuntime finds a bundled runtime under resources/worker', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-worker-'));
	try {
		const python = await fakeBundle(path.join(dir, 'worker'), 'linux');
		const runtime = resolveRuntime({ packaged: true, resourcesPath: dir, repoDir: '/nowhere', platform: 'linux' });
		assert.equal(runtime.mode, 'bundled');
		assert.equal(runtime.python, python);
		assert.equal(runtime.nodeBinDir, path.join(dir, 'worker', 'node', 'bin'));
		assert.equal(runtime.toolsDir, path.join(dir, 'worker', 'server', 'tools'));
		assert.equal(runtime.manifest.python.version, '3.12.14');
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('resolveRuntime explains a stub or missing bundle', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-worker-'));
	try {
		assert.match(resolveRuntime({ packaged: true, resourcesPath: dir, repoDir: '/nowhere' }).error, /missing/);
		await mkdir(path.join(dir, 'worker'), { recursive: true });
		await writeFile(path.join(dir, 'worker', 'manifest.json'), JSON.stringify({ stub: true }));
		assert.match(resolveRuntime({ packaged: true, resourcesPath: dir, repoDir: '/nowhere' }).error, /without a worker runtime/);
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('resolveRuntime uses the repository venv in development', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-repo-'));
	try {
		assert.match(resolveRuntime({ packaged: false, resourcesPath: '/x', repoDir: dir, platform: 'linux', env: {} }).error, /no venv/);
		await mkdir(path.join(dir, 'venv', 'bin'), { recursive: true });
		await writeFile(path.join(dir, 'venv', 'bin', 'python'), '');
		assert.match(resolveRuntime({ packaged: false, resourcesPath: '/x', repoDir: dir, platform: 'linux', env: {} }).error, /frontend build/);
		await mkdir(path.join(dir, 'app', 'static'), { recursive: true });
		await writeFile(path.join(dir, 'app', 'static', 'index.html'), '');
		const runtime = resolveRuntime({ packaged: false, resourcesPath: '/x', repoDir: dir, platform: 'linux', env: {} });
		assert.equal(runtime.mode, 'repo');
		assert.equal(runtime.serverDir, dir);
		assert.equal(runtime.nodeBinDir, null);
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('buildWorkerEnv preserves non-DB settings and drops ALL inherited DB settings case-insensitively', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-worker-'));
	try {
		await fakeBundle(path.join(dir, 'worker'), 'win32');
		const runtime = resolveRuntime({ packaged: true, resourcesPath: dir, repoDir: '/nowhere', platform: 'win32' });
		const env = buildWorkerEnv({
			runtime,
			dataDir: 'C:\\data',
			port: 8000,
			envFile: 'C:\\data\\.env',
			platform: 'win32',
			baseEnv: { Path: 'C:\\Windows', POSTGRES_HOST: 'leak', DATABASE_URL: 'leak', database_password: 'leak', DATABASE_POOL_SIZE: 'leak', PGPASSWORD: 'leak', PGHOST: 'leak', Postgres_Port: 'leak', PYTHONPATH: 'leak', HOME: 'h', CORS_ORIGINS: 'retained', WORKSPACE_DIR: 'retained-workspace', SECRET_KEY: 'retained-test-key' }
		});
		assert.equal(env.LOCAL_MODE, 'true');
		assert.equal(env.HOST, '127.0.0.1');
		assert.equal(env.PORT, '8000');
		assert.equal(Object.keys(env).some((key) => /^(DATABASE_|POSTGRES_|PG)/i.test(key)), false);
		assert.equal(env.DATA_DIR, 'C:\\data');
		assert.equal(env.WORKSPACE_DIR, 'retained-workspace');
		assert.equal(env.SECRET_KEY, 'retained-test-key');
		assert.equal(env.CORS_ORIGINS, 'retained');
		assert.equal(env.SHUFFLE_DESKTOP_PROTECTED_PORT, '8001');
		assert.equal(env.SHUFFLE_DESKTOP_ISOLATED, '0');
		assert.equal(env.SHUFFLE_PROTECTED_PORT, '8001');
		const isolated = buildWorkerEnv({ runtime, port: 18080, protectedPort: 18081, isolated: true, baseEnv: { shuffle_desktop_isolated: '0', shuffle_protected_port: '8001' } });
		assert.equal(isolated.SHUFFLE_DESKTOP_ISOLATED, '1');
		assert.equal(isolated.SHUFFLE_PROTECTED_PORT, '18081');
		assert.equal(isolated.shuffle_desktop_isolated, undefined);
		assert.equal(env.PYTHONPATH, runtime.serverDir);
		assert.equal(isolated.SHUFFLE_DESKTOP_PROTECTED_PORT, '18081');
		const repo = buildWorkerEnv({ runtime: { ...runtime, mode: 'repo' }, port: 8000 });
		assert.equal(repo.PYTHONPATH, path.join(runtime.serverDir, 'fluid'));
		assert.equal(env.HOME, 'h');
		assert.equal(env.TOOLS_DIR, runtime.toolsDir);
		assert.equal(env.BROWSER_MCP_ENTRY, runtime.browserMcpEntry);
		const parts = env.Path.split(';');
		assert.equal(parts[0], path.dirname(runtime.python));
		assert.equal(parts[1], runtime.nodeBinDir);
		assert.equal(parts[2], 'C:\\Windows');
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('D5: the packaged (non-preview) worker runs on the user\'s own home and Claude Code config dir', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-worker-'));
	try {
		for (const platform of ['win32', 'darwin', 'linux']) {
			await fakeBundle(path.join(dir, platform, 'worker'), platform);
			const runtime = resolveRuntime({ packaged: true, resourcesPath: path.join(dir, platform), repoDir: '/nowhere', platform });
			// No profileDir: that is only ever set for preview and isolated builds.
			const plain = buildWorkerEnv({ runtime, port: 8000, dataDir: '/d', envFile: '/d/.env', platform, baseEnv: { PATH: '/bin', HOME: '/home/me', USERPROFILE: 'C:\\Users\\me' } });
			assert.equal(plain.HOME, '/home/me', platform);
			assert.equal(plain.USERPROFILE, 'C:\\Users\\me', platform);
			assert.equal(plain.CLAUDE_CONFIG_DIR, undefined, `${platform}: the CLI falls back to ~/.claude of the real home`);
			const custom = buildWorkerEnv({ runtime, port: 8000, dataDir: '/d', envFile: '/d/.env', platform, baseEnv: { PATH: '/bin', HOME: '/home/me', CLAUDE_CONFIG_DIR: '/home/me/.config/claude' } });
			assert.equal(custom.CLAUDE_CONFIG_DIR, '/home/me/.config/claude', platform);
		}
	} finally {
		await rm(dir, { recursive: true, force: true });
	}
});

test('isPortFree tells a bound port from a free one', async () => {
	const server = net.createServer();
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const port = server.address().port;
	try {
		assert.equal(await isPortFree(port), false);
	} finally {
		await new Promise((resolve) => server.close(resolve));
	}
	assert.equal(await isPortFree(port), true);
	assert.equal(DEFAULT_PORT, 8000);
	assert.equal(PROTECTED_PORT, 8001);
});

// --- Supervisor: crash restart, attach identity, orphan cleanup -------------

const posix = process.platform !== 'win32';
const fakeWorker = fileURLToPath(new URL('./helpers/fake-worker.mjs', import.meta.url));

function freePort() {
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.once('error', reject);
		server.listen(0, '127.0.0.1', () => {
			const { port } = server.address();
			server.close(() => resolve(port));
		});
	});
}

test('isolated worker prepares redirected Windows AppData before spawning', async (t) => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-profile-'));
	const port = await freePort();
	const protectedPort = await freePort();
	const profileDir = path.join(dir, 'profile');
	const supervisor = new WorkerSupervisor({
		runtime: { mode: 'repo', root: dir, serverDir: dir, python: process.execPath, toolsDir: dir, browserMcpEntry: path.join(dir, 'index.js') },
		logDir: path.join(dir, 'logs'), dataDir: path.join(dir, 'data'), envFile: path.join(dir, 'worker', '.env'),
		profileDir, port, protectedPort, allowAttach: false, baseEnv: process.env,
		postgres: () => ({ host: '127.0.0.1', port: 5432, user: 'fixture', password: 'fixture', database: 'fixture' }),
	});
	t.after(async () => { await supervisor.stop(); await rm(dir, { recursive: true, force: true }); });
	// Node refuses Python's -m switch; no app or database connection is made.
	const state = await supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.equal(existsSync(path.join(profileDir, 'AppData', 'Roaming')), true);
	assert.equal(existsSync(path.join(profileDir, 'AppData', 'Local')), true);
});

/** A runtime whose "python" runs the fake worker; spawns are appended to `log`. */
async function fakeSupervisor(t, extraEnv = {}, options = {}) {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-supervisor-'));
	t.after(() => rm(dir, { recursive: true, force: true }));
	const python = path.join(dir, 'python');
	await writeFile(python, '#!/bin/sh\nexec "$FAKE_WORKER_NODE" "$FAKE_WORKER_SCRIPT"\n', { mode: 0o755 });
	await mkdir(path.join(dir, 'server'), { recursive: true });
	const runtime = { mode: 'bundled', root: dir, python, serverDir: path.join(dir, 'server'), nodeBinDir: null, toolsDir: path.join(dir, 'tools'), browserMcpEntry: path.join(dir, 'mcp.js'), manifest: null };
	const log = path.join(dir, 'spawns.log');
	const port = await freePort();
	const protectedPort = await freePort();
	const baseEnv = { PATH: process.env.PATH, FAKE_WORKER_NODE: process.execPath, FAKE_WORKER_SCRIPT: fakeWorker, FAKE_WORKER_LOG: log, ...extraEnv };
	const supervisor = new WorkerSupervisor({
		runtime, logDir: path.join(dir, 'logs'), envFile: path.join(dir, 'worker', '.env'), stateDir: path.join(dir, 'worker'),
		dataDir: path.join(dir, 'data'), port, protectedPort, baseEnv, readyTimeoutMs: 15_000,
		postgres: () => ({ host: '127.0.0.1', port: 5432, user: 'u', password: 'pw', database: 'd' }), ...options
	});
	const states = [];
	supervisor.on('state', (state) => states.push(state));
	t.after(async () => { await supervisor.stop().catch(() => {}); });
	const spawns = async () => { try { return (await readFile(log, 'utf8')).trim().split('\n').filter(Boolean); } catch { return []; } };
	const processFile = path.join(dir, 'worker', 'worker-process.json');
	return { dir, port, protectedPort, baseEnv, supervisor, states, spawns, processFile };
}

function waitFor(supervisor, predicate, timeoutMs = 30_000) {
	return new Promise((resolve, reject) => {
		if (predicate(supervisor.state)) return resolve(supervisor.state);
		const timer = setTimeout(() => { supervisor.off('state', check); reject(new Error(`Timed out waiting for worker state; last ${JSON.stringify(supervisor.state)}`)); }, timeoutMs);
		function check(state) { if (predicate(state)) { clearTimeout(timer); supervisor.off('state', check); resolve(state); } }
		supervisor.on('state', check);
	});
}

/** An in-test stand-in for a worker this app did not start. */
async function foreignWorker(t, port, health) {
	const server = http.createServer((req, res) => {
		res.setHeader('content-type', 'application/json');
		res.end(JSON.stringify(req.url === '/health' ? health : { status: req.url === '/api/v1/network/startup-sync' ? 'synced' : 'ready' }));
	});
	await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
	t.after(() => new Promise((resolve) => server.close(resolve)));
	return server;
}

test('a start that fails before the spawn reports failed instead of rejecting or staying starting', async (t) => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-logdir-'));
	t.after(() => rm(dir, { recursive: true, force: true }));
	await writeFile(path.join(dir, 'blocker'), '');
	const runtime = { mode: 'bundled', root: dir, python: path.join(dir, 'python'), serverDir: dir, nodeBinDir: null, toolsDir: dir, browserMcpEntry: dir, manifest: null };
	const supervisor = new WorkerSupervisor({
		runtime, logDir: path.join(dir, 'blocker', 'logs'), envFile: path.join(dir, 'worker', '.env'), dataDir: path.join(dir, 'data'),
		port: await freePort(), protectedPort: await freePort(), allowAttach: false,
		postgres: () => ({ host: '127.0.0.1', port: 5432, user: 'u', password: 'pw', database: 'd' })
	});
	const state = await supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.match(state.error, /ENOTDIR|EEXIST|not a directory/i);
	assert.equal(state.pid, null);
});

test('a worker log file that cannot be opened does not crash the app', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	// worker.log is a directory and cannot be rotated away (worker.prev.log is a non-empty directory).
	await mkdir(path.join(f.dir, 'logs', 'worker.log'), { recursive: true });
	await mkdir(path.join(f.dir, 'logs', 'worker.prev.log', 'keep'), { recursive: true });
	assert.equal((await f.supervisor.start()).phase, 'ready');
	assert.ok(f.supervisor.tail().some((line) => line.startsWith('--- worker start')), 'the in-memory tail still records the run');
});

test('DESK-01: a worker that crashes after becoming ready is respawned, twice per five minutes, then reported', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, { FAKE_WORKER_EXIT_AFTER_READY: '300' });
	const first = await f.supervisor.start();
	assert.equal(first.phase, 'ready');
	assert.equal(first.external, false);
	const firstPid = first.pid;
	const failed = await waitFor(f.supervisor, (s) => s.phase === 'failed', 60_000);
	assert.match(failed.error, /keeps stopping unexpectedly/);
	const spawns = await f.spawns();
	assert.equal(spawns.length, 3, `expected the original spawn plus two restarts, got ${spawns.join(' | ')}`);
	assert.equal(new Set(spawns.map((line) => line.split(' ')[0])).size, 3);
	assert.equal(spawns[0].split(' ')[0], String(firstPid));
	assert.equal(new Set(spawns.map((line) => line.split(' ')[1])).size, 3, 'every spawn gets a fresh attach token');
	const restarting = f.states.filter((s) => s.phase === 'starting' && /restarting it/.test(s.error ?? ''));
	assert.equal(restarting.length, 2);
	// Each restart actually reached ready again (the old early return left the phase stuck at starting).
	assert.equal(f.states.filter((s) => s.phase === 'ready').length, 3);
	assert.equal(f.supervisor.state.pid, null);
	await sleep(2000);
	assert.equal((await f.spawns()).length, 3, 'no further spawn after giving up');
});

test('DESK-01: stopping the app cancels a scheduled crash restart', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, { FAKE_WORKER_EXIT_AFTER_READY: '300' });
	await f.supervisor.start();
	await waitFor(f.supervisor, (s) => s.phase === 'starting' && /restarting it/.test(s.error ?? ''));
	await f.supervisor.stop();
	assert.equal(f.supervisor.state.phase, 'stopped');
	await sleep(2500);
	assert.equal((await f.spawns()).length, 1);
	assert.equal(f.supervisor.state.phase, 'stopped');
});

test('DESK-03: a worker this app did not start is not attached and nothing is spawned', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	await foreignWorker(t, f.port, { status: 'healthy', service: 'ai-shuffle', version: '1.0.0' });
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.equal(state.external, false);
	assert.match(state.error, /Port \d+ is in use by another AI Shuffle worker that this app did not start/);
	assert.match(state.error, /SHUFFLE_DESKTOP_ATTACH_ANY=1/);
	assert.deepEqual(await f.spawns(), []);
	assert.equal(existsSync(f.processFile), false);
});

test('DESK-03: a wrong attach hash is treated like no hash', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	await writePrivateFile(f.processFile, JSON.stringify({ version: 1, pid: null, token: 'a'.repeat(64), port: f.port }));
	await foreignWorker(t, f.port, { status: 'healthy', service: 'ai-shuffle', attach: attachTokenHash('b'.repeat(64)) });
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.match(state.error, /did not start/);
	assert.deepEqual(await f.spawns(), []);
});

test('DESK-03: a matching attach hash proves the worker is ours and it is adopted', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	const token = 'c'.repeat(64);
	await writePrivateFile(f.processFile, JSON.stringify({ version: 1, pid: null, token, port: f.port }));
	await foreignWorker(t, f.port, { status: 'healthy', service: 'ai-shuffle', attach: attachTokenHash(token) });
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'ready');
	assert.equal(state.external, true);
	assert.equal(state.url, `http://127.0.0.1:${f.port}`);
	assert.deepEqual(await f.spawns(), []);
});

test('DESK-03: SHUFFLE_DESKTOP_ATTACH_ANY=1 restores attaching to any local worker', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, { [ATTACH_ANY_ENV]: '1' });
	await foreignWorker(t, f.port, { status: 'healthy', service: 'ai-shuffle', version: '1.0.0' });
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'ready');
	assert.equal(state.external, true);
	assert.deepEqual(await f.spawns(), []);
	// A program that is not an AI Shuffle worker is still refused.
	const g = await fakeSupervisor(t, { [ATTACH_ANY_ENV]: '1' });
	await foreignWorker(t, g.port, { status: 'healthy', service: 'something-else' });
	const other = await g.supervisor.start();
	assert.equal(other.phase, 'failed');
	assert.match(other.error, /in use by another program/);
});

test('DESK-03: the attach token reaches the worker and never leaks in from the parent environment', async (t) => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-worker-'));
	t.after(() => rm(dir, { recursive: true, force: true }));
	await fakeBundle(path.join(dir, 'worker'), 'linux');
	const runtime = resolveRuntime({ packaged: true, resourcesPath: dir, repoDir: '/nowhere', platform: 'linux' });
	const env = buildWorkerEnv({ runtime, port: 8000, baseEnv: { PATH: '/bin', SHUFFLE_DESKTOP_ATTACH_TOKEN: 'inherited', shuffle_desktop_attach_token: 'inherited', SHUFFLE_DESKTOP_LISTENER_TOKEN: 'inherited', shuffle_desktop_listener_token: 'inherited' } });
	assert.equal(env.SHUFFLE_DESKTOP_ATTACH_TOKEN, undefined);
	assert.equal(env.SHUFFLE_DESKTOP_LISTENER_TOKEN, undefined);
	assert.equal(env.shuffle_desktop_listener_token, undefined);
	assert.equal(env.shuffle_desktop_attach_token, undefined);
	assert.equal(attachTokenHash('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
	assert.equal(await readWorkerProcessRecord(path.join(dir, 'missing.json')), null);
	await writeFile(path.join(dir, 'bad.json'), JSON.stringify({ version: 1, token: 'short' }));
	assert.equal(await readWorkerProcessRecord(path.join(dir, 'bad.json')), null);
});

test('SEC-08: every spawn gets a fresh listener token that the app sends and records privately', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, { SHUFFLE_DESKTOP_LISTENER_TOKEN: 'f'.repeat(64) });
	const state = await f.supervisor.start();
	// The fake worker refuses startup sync without the header, so ready proves the app sent it.
	assert.equal(state.phase, 'ready');
	const [spawn] = await f.spawns();
	const listener = spawn.split(' ')[2];
	assert.match(listener, /^[a-f0-9]{64}$/);
	assert.notEqual(listener, 'f'.repeat(64), 'an inherited token never reaches the worker');
	assert.equal(f.supervisor.listenerToken, listener);
	assert.equal(JSON.stringify(f.supervisor.state).includes(listener), false, 'worker state sent to pages never carries the token');
	assert.equal((await readWorkerProcessRecord(f.processFile)).listener, listener);
	await f.supervisor.stop();
	await f.supervisor.start();
	const second = (await f.spawns())[1].split(' ')[2];
	assert.notEqual(second, listener);
	assert.equal(f.supervisor.listenerToken, second);
});

test('SEC-08: Open in Browser asks the worker for a one-time session URL with the listener token', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	assert.equal(await f.supervisor.browserHandoffUrl(), null, 'nothing before the worker is ready');
	assert.equal((await f.supervisor.start()).phase, 'ready');
	// The fake worker refuses the handoff without the header, so a URL proves the app sent it.
	assert.equal(await f.supervisor.browserHandoffUrl(), `http://127.0.0.1:${f.port}/api/v1/owner/handoff?token=one-time`);
	assert.equal(await requestBrowserHandoff(f.port, null), null, 'no token, no handoff: the plain URL works');
	await assert.rejects(requestBrowserHandoff(f.port, 'e'.repeat(64)), /403/);
});

test('SEC-08: an adopted worker of ours keeps its recorded listener token; a foreign one gets none', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	const token = 'c'.repeat(64);
	const listener = 'd'.repeat(64);
	await writePrivateFile(f.processFile, JSON.stringify({ version: 1, pid: null, token, port: f.port, listener }));
	await foreignWorker(t, f.port, { status: 'healthy', service: 'ai-shuffle', attach: attachTokenHash(token) });
	assert.equal((await f.supervisor.start()).external, true);
	assert.equal(f.supervisor.listenerToken, listener);
	const g = await fakeSupervisor(t, { [ATTACH_ANY_ENV]: '1' });
	await foreignWorker(t, g.port, { status: 'healthy', service: 'ai-shuffle' });
	assert.equal((await g.supervisor.start()).external, true);
	assert.equal(g.supervisor.listenerToken, null);
	await writeFile(path.join(g.dir, 'bad-listener.json'), JSON.stringify({ version: 1, token, listener: 'short' }));
	assert.equal((await readWorkerProcessRecord(path.join(g.dir, 'bad-listener.json'))).listener, undefined);
});

test('DESK-04: an orphaned worker from a previous run is stopped by pid and a fresh one started', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	const token = 'd'.repeat(64);
	const orphan = spawn(process.execPath, [fakeWorker], { env: { ...f.baseEnv, PORT: String(f.port), SHUFFLE_DESKTOP_ATTACH_TOKEN: token }, detached: true, stdio: 'ignore' });
	t.after(() => { try { process.kill(-orphan.pid, 'SIGKILL'); } catch { /* gone */ } });
	const orphanExit = new Promise((resolve) => orphan.once('exit', resolve));
	for (let i = 0; i < 50 && !(await fetchHealth(f.port)).ok; i++) await sleep(100);
	assert.equal((await fetchHealth(f.port)).attach, attachTokenHash(token));
	await writePrivateFile(f.processFile, JSON.stringify({ version: 1, pid: orphan.pid, token, port: f.port }));
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'ready');
	assert.equal(state.external, false, 'the orphan is not adopted');
	assert.notEqual(state.pid, orphan.pid);
	await orphanExit;
	const spawns = await f.spawns();
	assert.equal(spawns.length, 2);
	assert.equal(spawns[0].split(' ')[0], String(orphan.pid));
	assert.equal(spawns[1].split(' ')[0], String(state.pid));
	const record = JSON.parse(await readFile(f.processFile, 'utf8'));
	assert.equal(record.pid, state.pid);
	assert.equal((await fetchHealth(f.port)).attach, attachTokenHash(record.token), 'the record holds the running worker\'s token');
	await f.supervisor.stop();
	assert.equal(f.supervisor.state.phase, 'stopped');
	for (let i = 0; i < 20 && existsSync(f.processFile); i++) await sleep(50);
	assert.equal(existsSync(f.processFile), false, 'a clean stop removes the process record');
});

test('DESK-04: a preview build also reclaims its own orphan but still never attaches to anything else', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, {}, { allowAttach: false });
	const token = 'e'.repeat(64);
	const orphan = spawn(process.execPath, [fakeWorker], { env: { ...f.baseEnv, PORT: String(f.port), SHUFFLE_DESKTOP_ATTACH_TOKEN: token }, detached: true, stdio: 'ignore' });
	t.after(() => { try { process.kill(-orphan.pid, 'SIGKILL'); } catch { /* gone */ } });
	const orphanExit = new Promise((resolve) => orphan.once('exit', resolve));
	for (let i = 0; i < 50 && !(await fetchHealth(f.port)).ok; i++) await sleep(100);
	await writePrivateFile(f.processFile, JSON.stringify({ version: 1, pid: orphan.pid, token, port: f.port }));
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'ready');
	assert.equal(state.external, false);
	await orphanExit;
	assert.equal((await f.spawns()).length, 2);
	await f.supervisor.stop();
	// A record naming a live pid that is NOT ours (hash mismatch) is left alone and the port stays refused.
	const g = await fakeSupervisor(t, {}, { allowAttach: false });
	await writePrivateFile(g.processFile, JSON.stringify({ version: 1, pid: process.pid, token: 'f'.repeat(64), port: g.port }));
	await foreignWorker(t, g.port, { status: 'healthy', service: 'ai-shuffle', attach: attachTokenHash('0'.repeat(64)) });
	const refused = await g.supervisor.start();
	assert.equal(refused.phase, 'failed');
	assert.match(refused.error, /in use by another program/);
	assert.deepEqual(await g.spawns(), []);
});

// --- Supervisor: start-up failures, port collisions, log rotation ----------

/** A server on `port` that answers each request with `reply(req, res)`. */
async function rawServer(t, port, reply) {
	const server = http.createServer(reply);
	await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
	t.after(() => new Promise((resolve) => { server.closeAllConnections?.(); server.close(resolve); }));
	return server;
}

const within = (promise, ms, what) => Promise.race([promise, sleep(ms).then(() => { throw new Error(`${what} did not settle within ${ms} ms`); })]);

test('a health reply cut off mid-body resolves as not ok instead of hanging the probe', async (t) => {
	const port = await freePort();
	await rawServer(t, port, (req, res) => {
		res.writeHead(200, { 'content-type': 'application/json', 'content-length': '1000' });
		res.write('{"service":');
		setTimeout(() => req.socket.destroy(), 20);
	});
	const reply = await within(fetchHealth(port), 2000, 'fetchHealth');
	assert.equal(reply.ok, false);
});

test('a health reply that never stops streaming is cut off at a bound', async (t) => {
	const port = await freePort();
	await rawServer(t, port, (_req, res) => {
		res.writeHead(200, { 'content-type': 'application/json' });
		// Faster than the probe's idle timeout, so only the size bound can end it.
		const timer = setInterval(() => { if (!res.write('x'.repeat(8192))) { /* keep going */ } }, 5);
		res.on('close', () => clearInterval(timer));
	});
	const reply = await within(fetchHealth(port, '/health', 60_000), 5000, 'fetchHealth');
	assert.equal(reply.ok, false);
});

/** A responder that drips a byte at a time: never idle long enough for the http timeout, never over the size cap. */
function dripServer(t, port) {
	return rawServer(t, port, (_req, res) => {
		res.writeHead(200, { 'content-type': 'application/json' });
		res.write('{"service":"ai-shuffle"');
		const timer = setInterval(() => res.write(' '), 50);
		res.on('close', () => clearInterval(timer));
	});
}

test('a health reply that drips bytes forever is ended by the overall deadline, with a reason', async (t) => {
	const port = await freePort();
	await dripServer(t, port);
	assert.equal(HEALTH_TOTAL_TIMEOUT_MS, 5000);
	const started = Date.now();
	const reply = await within(fetchHealth(port, '/health', 2500, 400), 3000, 'fetchHealth');
	assert.equal(reply.ok, false);
	assert.match(reply.reason, /did not finish within/);
	assert.ok(Date.now() - started < 2000, 'ended at the overall deadline, not by the idle timeout');
});

test('a worker port held by a dripping responder fails the start and says the health check timed out', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, {}, { healthTotalMs: 400 });
	await dripServer(t, f.port);
	const state = await within(f.supervisor.start(), 3000, 'start');
	assert.equal(state.phase, 'failed');
	assert.match(state.error, new RegExp(`Port ${f.port} is in use by another program\\. The health check did not finish within`));
	assert.deepEqual(await f.spawns(), []);
});

test('a worker port answering with a truncated health reply fails the start instead of leaving it starting', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	await rawServer(t, f.port, (req, res) => {
		res.writeHead(200, { 'content-type': 'application/json', 'content-length': '1000' });
		res.write('{"service":"ai-shuffle"');
		setTimeout(() => req.socket.destroy(), 20);
	});
	const state = await within(f.supervisor.start(), 5000, 'start');
	assert.equal(state.phase, 'failed');
	assert.match(state.error, new RegExp(`Port ${f.port} is in use by another program`));
	assert.deepEqual(await f.spawns(), []);
	// And stop() does not wait on a start that never ends.
	await within(f.supervisor.stop(), 5000, 'stop');
});

test('the protected port held by another program fails the start before anything is spawned', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, {}, { allowAttach: false });
	const holder = net.createServer();
	await new Promise((resolve) => holder.listen(f.protectedPort, '127.0.0.1', resolve));
	t.after(() => new Promise((resolve) => holder.close(resolve)));
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.match(state.error, new RegExp(`Port ${f.protectedPort} is in use by another program`));
	assert.equal(state.pid, null);
	assert.deepEqual(await f.spawns(), []);
	assert.equal(existsSync(f.processFile), false);
	// Once the port is free, a retry starts normally.
	await new Promise((resolve) => holder.close(resolve));
	assert.equal((await f.supervisor.start()).phase, 'ready');
});

test('no embedded database means no spawn and a stated reason', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, {}, { postgres: () => null });
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.match(state.error, /embedded PostgreSQL server is not ready/);
	assert.deepEqual(await f.spawns(), []);
});

test('a worker that never becomes ready is stopped at the deadline and reported, not left running', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, { FAKE_WORKER_NEVER_READY: '1' }, { readyTimeoutMs: 1500 });
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.match(state.error, /did not become ready in time/);
	assert.equal(state.pid, null);
	const [spawned] = await f.spawns();
	const pid = Number(spawned.split(' ')[0]);
	let alive = true;
	for (let i = 0; i < 20 && alive; i++) { try { process.kill(pid, 0); await sleep(50); } catch { alive = false; } }
	assert.equal(alive, false, 'the stuck worker process is gone');
	assert.ok(f.supervisor.tail().some((line) => /did not become ready in time; stopping it/.test(line)));
	for (let i = 0; i < 20 && existsSync(f.processFile); i++) await sleep(50);
	assert.equal(existsSync(f.processFile), false, 'no process record is left for the next launch to chase');
	await sleep(1800);
	assert.equal((await f.spawns()).length, 1, 'a start-up failure is not retried on its own');
});

test('a worker that exits while starting is reported with its exit code and last output, without a restart', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, { FAKE_WORKER_EXIT_BEFORE_READY: '4' });
	const state = await f.supervisor.start();
	assert.equal(state.phase, 'failed');
	assert.equal(state.exitCode, 4);
	assert.match(state.error, /stopped while starting\. \(exit code 4\)/);
	// The last lines are the worker's own output, not the supervisor's exit marker.
	assert.match(state.error, /fatal: could not parse settings$/);
	assert.doesNotMatch(state.error, /--- worker/);
	await sleep(1800);
	assert.equal((await f.spawns()).length, 1, 'crash restarts are only for a worker that was ready');
	assert.equal(f.states.some((s) => /restarting it/.test(s.error ?? '')), false);
});

test('each start rotates the previous run\'s log to worker.prev.log', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	const logs = path.join(f.dir, 'logs');
	await f.supervisor.start();
	const firstPid = f.supervisor.state.pid;
	await f.supervisor.stop();
	await f.supervisor.start();
	await f.supervisor.stop();
	const previous = await readFile(path.join(logs, 'worker.prev.log'), 'utf8');
	const current = await readFile(path.join(logs, 'worker.log'), 'utf8');
	assert.equal((previous.match(/--- worker start/g) ?? []).length, 1);
	assert.equal((current.match(/--- worker start/g) ?? []).length, 1);
	assert.match(previous, /--- worker stopped by the app/);
	assert.match(current, /--- worker stopped by the app/);
	assert.notEqual(previous, current);
	assert.ok(firstPid);
});

test('worker.log rotates to worker.prev.log when it passes the size cap during a run', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t, {}, { logMaxBytes: 4096 });
	const logs = path.join(f.dir, 'logs');
	assert.equal((await f.supervisor.start()).phase, 'ready');
	for (let i = 0; i < 5 && f.supervisor.logStream?.pending; i++) await sleep(50);
	// Stand in for a chatty worker: its output goes through the same path.
	for (let i = 0; i < 20; i++) f.supervisor.log(`line ${i} ${'x'.repeat(300)}`);
	f.supervisor.log('after the cap');
	await f.supervisor.stop();
	const previous = await readFile(path.join(logs, 'worker.prev.log'), 'utf8');
	const current = await readFile(path.join(logs, 'worker.log'), 'utf8');
	assert.match(previous, /--- worker start/, 'this run\'s early output moved to worker.prev.log');
	assert.match(current, /passed 4096 bytes; earlier output of this run is in worker.prev.log/);
	assert.match(current, /after the cap/);
	assert.match(current, /--- worker stopped by the app/, 'the stream stays usable after rotating');
	assert.ok((await stat(path.join(logs, 'worker.log'))).size < 4096 + 1024);
	// Nothing is lost across the rotation.
	for (let i = 0; i < 20; i++) assert.ok(previous.includes(`line ${i} `) || current.includes(`line ${i} `), `line ${i} kept`);
});

test('a child that exits after a newer one was spawned leaves the newer worker\'s state alone', { skip: !posix }, async (t) => {
	const f = await fakeSupervisor(t);
	assert.equal((await f.supervisor.start()).phase, 'ready');
	const stale = f.supervisor.child;
	// Stand in for a newer spawn (the stale child outlived its stop).
	const newer = { exitCode: null, pid: null, once() {}, kill() {} };
	f.supervisor.child = newer;
	const exited = new Promise((resolve) => stale.once('exit', resolve));
	process.kill(stale.pid, 'SIGKILL');
	await exited;
	await sleep(50);
	assert.equal(f.supervisor.child, newer, 'the handle a later stop needs is kept');
	assert.equal(f.supervisor.state.phase, 'ready');
	assert.equal(existsSync(f.processFile), true, 'the newer worker\'s process record stays');
	assert.equal((await f.spawns()).length, 1, 'no crash restart for a child that is not current');
	newer.exitCode = 0;
});

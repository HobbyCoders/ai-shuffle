import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import http from 'node:http';
import { tmpdir } from 'node:os';
import net from 'node:net';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { resolveRuntime, buildWorkerEnv, isPortFree, fetchHealth, attachTokenHash, readWorkerProcessRecord, WorkerSupervisor, ATTACH_ANY_ENV, DEFAULT_PORT, PROTECTED_PORT } = require('../dist/main/worker.js');
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
	const env = buildWorkerEnv({ runtime, port: 8000, baseEnv: { PATH: '/bin', SHUFFLE_DESKTOP_ATTACH_TOKEN: 'inherited', shuffle_desktop_attach_token: 'inherited' } });
	assert.equal(env.SHUFFLE_DESKTOP_ATTACH_TOKEN, undefined);
	assert.equal(env.shuffle_desktop_attach_token, undefined);
	assert.equal(attachTokenHash('abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
	assert.equal(await readWorkerProcessRecord(path.join(dir, 'missing.json')), null);
	await writeFile(path.join(dir, 'bad.json'), JSON.stringify({ version: 1, token: 'short' }));
	assert.equal(await readWorkerProcessRecord(path.join(dir, 'bad.json')), null);
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

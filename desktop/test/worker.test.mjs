import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import net from 'node:net';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { resolveRuntime, buildWorkerEnv, isPortFree, DEFAULT_PORT, PROTECTED_PORT } = require('../dist/main/worker.js');

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

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { getConfig, validateConfiguration } from './helpers/builder-config.mjs';
const require = createRequire(import.meta.url);
const { desktopIdentity, stableDataDir } = require('../dist/main/identity.js');
const { WorkerSupervisor, buildWorkerEnv, writeManagedPostgres, prepareWorkerConfig } = require('../dist/main/worker.js');
const runtime = { mode: 'bundled', root: '/bundle', python: '/bundle/python', serverDir: '/bundle/server', nodeBinDir: '/bundle/node', toolsDir: '/bundle/tools', browserMcpEntry: '/bundle/browser/index.js' };
const config = { host: '127.0.0.1', port: 54321, user: 'ai_shuffle', database: 'ai_shuffle', password: 'generated-test-only' };

function loadMain(name, replacements = {}, globals = {}) {
	const file = new URL(`../dist/main/${name}.js`, import.meta.url);
	const localRequire = createRequire(file);
	const module = { exports: {} };
	vm.runInNewContext(readFileSync(file, 'utf8'), {
		module, exports: module.exports, require: (id) => id in replacements ? replacements[id] : localRequire(id),
		process, Buffer, console, setTimeout, clearTimeout, setInterval, clearInterval, setImmediate, ...globals
	}, { filename: file.pathname });
	return module.exports;
}

test('preview identity comes exclusively from packaged metadata and installer never captures stable protocol/update feed', async () => {
	assert.equal(desktopIdentity(false, { shuffleDesktopChannel: 'preview' }).preview, false);
	assert.equal(desktopIdentity(true, {}).preview, false);
	const preview = desktopIdentity(true, { shuffleDesktopChannel: 'preview' });
	const stable = desktopIdentity(true, {});
	assert.equal(preview.name, 'AI Shuffle Preview');
	assert.notEqual(preview.appId, stable.appId); assert.notEqual(preview.protocol, stable.protocol);
	assert.equal(preview.allowAttach, false); assert.equal(preview.updates, false);
	assert.equal(preview.port, 8000); assert.equal(preview.protectedPort, 8001);
	const project = fileURLToPath(new URL('..', import.meta.url));
	const config = await getConfig(project, 'electron-builder.preview.yml', null);
	await validateConfiguration(config, { isEnabled: false });
	assert.equal(config.appId, preview.appId); assert.equal(config.productName, preview.name);
	// NC-16: no URL scheme is registered by either build; the stable one is checked too.
	assert.equal(config.protocols ?? null, null);
	const stableConfig = await getConfig(project, 'electron-builder.yml', null);
	assert.equal(stableConfig.protocols ?? null, null);
	assert.equal(config.publish, null); assert.equal(config.extraMetadata.shuffleDesktopChannel, 'preview');
	// A standalone preview must not inherit package.json's source launcher entry.
	assert.equal(config.extraMetadata.main, 'dist/main/index.js');
	assert.equal(config.nsis.runAfterFinish, false); assert.equal(config.nsis.deleteAppDataOnUninstall, false);
	assert.equal(config.nsis.shortcutName, preview.name); assert.equal(config.win.executableName, preview.name);
	assert.match(config.artifactName, /^AI-Shuffle-Preview-/);
});

test('preview pins all data/config/credential homes and ignores inherited DB env without changing stable settings', () => {
	const dir = path.join(tmpdir(), 'preview-test'); const profileDir = path.join(dir, 'profile');
	const baseEnv = { Path: '/keep', DATA_DIR: '/stable', data_dir: '/other-stable', WORKSPACE_DIR: '/stable', HOME: '/stable', USERPROFILE: '/stable', APPDATA: '/stable', LOCALAPPDATA: '/stable', AI_SHUFFLE_ENV_FILE: '/stable/.env', CLAUDE_PROJECTS_DIR: '/stable', FLUID_PROJECTS_DIR: '/stable', FLUID_SKILLS_DIR: '/stable', CODEX_HOME: '/stable', ANTHROPIC_API_KEY: 'inherited-test-only', SECRET_KEY: 'inherited-test-only', INSTANCE_ID: 'stable', DATABASE_URL: 'ignored-test-only', database_password: 'ignored-test-only', Postgres_HOST: 'ignored-test-only', PGPASSWORD: 'ignored-test-only', CHROME_CDP_PORT: '9221', CORS_ORIGINS: 'unchanged' };
	const env = buildWorkerEnv({ runtime, port: 8000, dataDir: path.join(dir, 'data'), workspaceDir: path.join(dir, 'workspace'), envFile: path.join(dir, 'worker', '.env'), profileDir, baseEnv, platform: 'win32' });
	assert.equal(env.DATA_DIR, path.join(dir, 'data')); assert.equal(env.WORKSPACE_DIR, path.join(dir, 'workspace'));
	assert.equal(env.AI_SHUFFLE_ENV_FILE, path.join(dir, 'worker', '.env'));
	for (const key of ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'CODEX_HOME', 'GH_CONFIG_DIR']) assert.ok(env[key].startsWith(profileDir), key);
	// The Claude CLI keeps the user's own login: its config dir is the host ~/.claude.
	assert.equal(env.CLAUDE_CONFIG_DIR, path.join('/stable', '.claude'));
	const inherited = buildWorkerEnv({ runtime, port: 8000, dataDir: path.join(dir, 'data'), envFile: path.join(dir, 'worker', '.env'), profileDir, baseEnv: { ...baseEnv, CLAUDE_CONFIG_DIR: '/custom-claude' }, platform: 'win32' });
	assert.equal(inherited.CLAUDE_CONFIG_DIR, '/custom-claude');
	for (const key of ['CLAUDE_PROJECTS_DIR', 'FLUID_PROJECTS_DIR', 'FLUID_SKILLS_DIR', 'ANTHROPIC_API_KEY', 'SECRET_KEY', 'INSTANCE_ID', 'data_dir']) assert.equal(env[key], undefined, key);
	assert.equal(Object.keys(env).some((key) => /^(DATABASE_|POSTGRES_|PG)/i.test(key)), false);
	assert.equal(env.CHROME_CDP_PORT, '19221'); assert.equal(env.CORS_ORIGINS, 'unchanged');
	assert.equal(baseEnv.DATA_DIR, '/stable', 'input is not mutated');
	assert.equal(stableDataDir('win32', { USERPROFILE: 'C:/Users/test' }, '/home'), path.join('C:/Users/test', '.ai-shuffle'));
	assert.equal(stableDataDir('linux', { DATA_DIR: '/existing' }, '/home'), '/existing');
	assert.equal(stableDataDir('linux', { XDG_DATA_HOME: '/existing' }, '/home'), path.join('/existing', 'ai-shuffle'));
});

test('managed descriptor is private, exact, atomic, reusable, and rejects external endpoints', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-descriptor-')); const file = path.join(dir, 'managed-postgres.json');
	try {
		await writeManagedPostgres(dir, { ...config, managed: true });
		assert.deepEqual(JSON.parse(await readFile(file, 'utf8')), { version: 1, ...config });
		await writeManagedPostgres(dir, { ...config, port: 54322 });
		assert.equal(JSON.parse(await readFile(file, 'utf8')).port, 54322);
		assert.deepEqual(await readdir(dir), ['managed-postgres.json']);
		await assert.rejects(writeManagedPostgres(dir, { ...config, host: 'external' }), /invalid/);
		assert.equal(JSON.parse(await readFile(file, 'utf8')).port, 54322);
		if (process.platform !== 'win32') {
			assert.equal((await stat(dir)).mode & 0o777, 0o700); assert.equal((await stat(file)).mode & 0o777, 0o600);
		}
	} finally { await rm(dir, { recursive: true, force: true }); }
});

test('worker config strips legacy database entries while preserving unrelated persisted settings across starts', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-config-')); const file = path.join(dir, '.env');
	try {
		await writeFile(file, '# keep\nPORT=8000\nMODEL=keep-me\nPOSTGRES_PASSWORD=old-test\nexport database_url=old-test\nPOSTGRES_HOST=old-test\n');
		await prepareWorkerConfig(file);
		const clean = await readFile(file, 'utf8');
		assert.equal(clean, '# keep\nPORT=8000\nMODEL=keep-me\n');
		await prepareWorkerConfig(file); assert.equal(await readFile(file, 'utf8'), clean);
	} finally { await rm(dir, { recursive: true, force: true }); }
});

test('preview refuses an existing healthy worker without attaching or writing its connection', async () => {
	let requests = 0;
	const server = http.createServer((_req, res) => { requests++; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({ service: 'ai-shuffle' })); });
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-refuse-'));
	try {
		const supervisor = new WorkerSupervisor({ runtime, logDir: dir, envFile: path.join(dir, '.env'), dataDir: dir, port: server.address().port, allowAttach: false, postgres: () => config });
		const state = await supervisor.start(); assert.equal(state.phase, 'failed'); assert.equal(state.external, false);
		assert.match(state.error, /in use/); assert.equal(requests, 0, 'preview does not even probe an unrelated worker');
		assert.deepEqual(await readdir(dir), []);
	} finally { await new Promise((resolve) => server.close(resolve)); await rm(dir, { recursive: true, force: true }); }
});

test('preview updater cannot register, check, download, install, or schedule updates', async () => {
	const calls = []; const autoUpdater = { on: () => calls.push('on'), checkForUpdates: () => calls.push('check'), quitAndInstall: () => calls.push('install') };
	const { Updater } = loadMain('updater', { electron: { app: { isPackaged: true } }, 'electron-updater': { autoUpdater } }, { setTimeout: () => calls.push('timeout'), setInterval: () => calls.push('interval') });
	const updater = new Updater(false); updater.useApplications({ check: () => calls.push('source-check'), download: () => calls.push('source-download') }); updater.start(); await updater.check(); await updater.download(); await updater.install(); updater.stop();
	assert.equal(updater.current.phase, 'unsupported'); assert.equal(autoUpdater.autoDownload, undefined); assert.equal(autoUpdater.autoInstallOnAppQuit, undefined);
	assert.deepEqual(calls, []);
});

test('update preload commands return promises and IPC rejects senders outside the main window', async () => {
 const handlers = new Map(); const calls = [];
 const { registerIpc } = loadMain('ipc', { electron: { ipcMain: { handle: (name, handler) => handlers.set(name, handler), on() {} }, app: {} } });
 registerIpc({ window: { isOwnSender: id => id === 7, currentWorkerUrl: 'http://127.0.0.1:8000', bundledPageUrl: 'file:///app/src/pages/worker.html' }, updater: {
  check: async interactive => calls.push(['check', interactive]), install: async () => calls.push(['install'])
 }, worker: {} });
 const exposed = {}; let sender = 7;
 const frame = { url: 'http://127.0.0.1:8000/', processId: 1, routingId: 1 };
 vm.runInNewContext(readFileSync(new URL('../dist/preload/index.js', import.meta.url), 'utf8'), { exports: {}, process, window: { location: { protocol: 'https:' } }, require: () => ({
  contextBridge: { exposeInMainWorld: (name, value) => { exposed[name] = value; } },
  ipcRenderer: { sendSync: () => null, invoke: async channel => handlers.get(channel)({ sender: { id: sender, mainFrame: frame }, senderFrame: frame }) }
 }) });
 const updates = exposed.shuffleDesktop.updates;
 // NC-15: the web UI downloads through the worker's update bridge; the preload has no download member or channel.
 assert.equal('download' in updates, false); assert.equal(handlers.has('desktop:update-download'), false);
 for (const action of ['check', 'install']) { const result = updates[action](); assert.equal(typeof result.then, 'function'); await result; }
 assert.deepEqual(calls, [['check', true], ['install']]);
 sender = 8;
 for (const action of ['check', 'install']) await assert.rejects(updates[action](), /not allowed/);
 assert.equal(calls.length, 2);
});

test('IPC and preload contain no external database actions, even on bundled pages', () => {
	const handlers = new Map(); const listeners = new Map();
	const { registerIpc } = loadMain('ipc', { electron: { ipcMain: { handle: (name, handler) => handlers.set(name, handler), on: (name, handler) => listeners.set(name, handler) }, app: {} } });
	registerIpc({ window: { isOwnSender: () => true }, updater: {}, worker: {} });
	for (const name of ['desktop:db-check', 'desktop:settings-get', 'desktop:setup-save', 'desktop:setup-use-bundled', 'desktop:setup-cancel', 'desktop:worker-open-setup']) assert.equal(handlers.has(name), false, name);
	const exposed = {};
	vm.runInNewContext(readFileSync(new URL('../dist/preload/index.js', import.meta.url), 'utf8'), { exports: {}, process, window: { location: { protocol: 'file:' } }, require: () => ({ contextBridge: { exposeInMainWorld: (name, value) => { exposed[name] = value; } }, ipcRenderer: { sendSync: () => null } }) });
	const bridge = exposed.shuffleDesktopWorker;
	for (const name of ['getSettings', 'checkDatabase', 'saveAndStart', 'useBundled', 'cancelSetup', 'openSetup']) assert.equal(name in bridge, false, name);
	assert.deepEqual(Object.keys(exposed.shuffleDesktop.worker), ['getState']);
});

test('only the bundled status page in the main window can open the worker UI', async () => {
	const handlers = new Map(); let opened = 0;
	const { registerIpc } = loadMain('ipc', { electron: { ipcMain: { handle: (name, handler) => handlers.set(name, handler), on() {} }, app: {} } });
	registerIpc({ window: { isOwnSender: id => id === 7, currentWorkerUrl: null, bundledPageUrl: 'file:///app/src/pages/worker.html' }, updater: {}, worker: { openUi: async () => { opened++; } } });
	const exposed = {}; let event;
	vm.runInNewContext(readFileSync(new URL('../dist/preload/index.js', import.meta.url), 'utf8'), { exports: {}, process, window: { location: { protocol: 'file:' } }, require: () => ({
		contextBridge: { exposeInMainWorld: (name, value) => { exposed[name] = value; } },
		ipcRenderer: { sendSync: () => null, invoke: async channel => handlers.get(channel)(event) }
	}) });
	const main = (url, id = 7) => { const frame = { url, processId: 1, routingId: 1 }; return { sender: { id, mainFrame: frame }, senderFrame: frame }; };
	event = main('file:///app/src/pages/worker.html?mode=status');
	await exposed.shuffleDesktopWorker.openUi(); assert.equal(opened, 1);
	for (const denied of [main('http://127.0.0.1:8000/'), main('file:///app/src/pages/worker.html', 8), main('file:///tmp/other.html')]) {
		event = denied; await assert.rejects(exposed.shuffleDesktopWorker.openUi(), /not allowed/);
	}
	assert.equal(opened, 1);
	assert.equal('openUi' in exposed.shuffleDesktop.worker, false, 'the web UI bridge is unchanged');
});

test('closing during private descriptor preparation cancels startup and joins cleanup before returning', async () => {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-cancel-'));
	let entered; const writing = new Promise((resolve) => { entered = resolve; });
	let release; const unblock = new Promise((resolve) => { release = resolve; }); let spawns = 0;
	const realWrite = require('../dist/main/private-file.js').writePrivateFile;
	const { WorkerSupervisor: Cancellable } = loadMain('worker', {
		'./private-file': { writePrivateFile: async (file, content) => { entered(); await unblock; await realWrite(file, content); } },
		'node:child_process': { spawn: () => { spawns++; throw Error('must not spawn'); } },
		'node:net': { createServer: () => ({ once() {}, listen(_options, callback) { callback(); }, close(callback) { callback(); } }) }
	});
	try {
		const worker = new Cancellable({ runtime, logDir: dir, envFile: path.join(dir, '.env'), dataDir: dir, postgres: () => config, allowAttach: false });
		const start = worker.start(); await writing;
		const stop = worker.stop(); release(); await Promise.all([start, stop]);
		assert.equal(spawns, 0); assert.equal(worker.state.phase, 'stopped');
		await worker.clearConnectionFile(); await assert.rejects(readFile(path.join(dir, 'managed-postgres.json')), { code: 'ENOENT' });
	} finally { await rm(dir, { recursive: true, force: true }); }
});

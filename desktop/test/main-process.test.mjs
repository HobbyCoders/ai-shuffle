import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const desktopDir = fileURLToPath(new URL('..', import.meta.url));
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const URL_READY = 'http://127.0.0.1:8000';

/** Runs dist/main/index.js against fakes of Electron and of every module with side effects. */
async function launch({ stop = async () => {}, acknowledge = async () => true } = {}) {
	const file = path.join(desktopDir, 'dist/main/index.js');
	const localRequire = createRequire(file);
	const appEvents = new EventEmitter();
	const errors = [];
	const app = {
		isPackaged: false,
		getAppPath: () => desktopDir,
		getPath: () => path.join(desktopDir, 'no-user-data'),
		getVersion: () => '0.0.0-test',
		setAppUserModelId() {}, setName() {}, setPath() {},
		requestSingleInstanceLock: () => true,
		on: (name, fn) => appEvents.on(name, fn),
		whenReady: async () => {},
		quits: 0,
		quit() { app.quits++; }, relaunch() {}
	};
	const pages = [];
	let mainWindow;
	class MainWindow {
		constructor() { mainWindow = this; this.browserWindow = null; this.page = null; this.workerUrl = null; this.sent = []; }
		get currentPage() { return this.page; }
		get currentWorkerUrl() { return this.page === null ? this.workerUrl : null; }
		async showPage(mode) { this.browserWindow = {}; this.page = mode; pages.push(mode); }
		async loadWorker(url) { this.browserWindow = {}; this.page = null; this.workerUrl = url; pages.push(url); }
		focus() {}
		onLoadFailed() {}
		send(channel, payload) { this.sent.push([channel, payload]); }
		openExternal() {}
	}
	let worker;
	class WorkerSupervisor extends EventEmitter {
		constructor() {
			super();
			worker = this;
			this.state = { phase: 'idle', port: 8000, url: URL_READY, pid: null, external: false, error: null, exitCode: null, startedAt: null, logPath: '' };
			this.listenerToken = null;
		}
		async start() {
			if (this.state.phase === 'ready') return this.state;
			this.state = { ...this.state, phase: 'ready' };
			this.emit('state', this.state);
			return this.state;
		}
		async stop() { await stop(); this.state = { ...this.state, phase: 'stopped' }; this.emit('state', this.state); }
		tail() { return []; }
		async activeSessionCount() { return 0; }
		async clearConnectionFile() {}
	}
	class PostgresSupervisor extends EventEmitter {
		constructor() { super(); this.state = { phase: 'idle', port: null, error: null, logPath: '' }; }
		async start() { this.state = { ...this.state, phase: 'ready', port: 5432 }; return this.state; }
		async stop() {}
		connection() { return {}; }
		tail() { return []; }
	}
	class Updater {
		setPolicy() {} setBridge() {} subscribe() { return () => {}; } onBeforeInstall() {} start() {} stop() {}
		async check() {}
	}
	let menu;
	const replacements = {
		electron: {
			app,
			dialog: { showErrorBox: (title, message) => errors.push([title, message]), showMessageBox: async () => ({ response: 0 }) },
			safeStorage: { isEncryptionAvailable: () => false },
			session: { fromPartition: () => ({ setUserAgent() {}, getUserAgent: () => 'test' }) },
			shell: { showItemInFolder() {}, openPath: async () => '' }
		},
		'./close-dialog': { showCloseDialog: async () => true },
		'./managed-context': { managedContext: () => null },
		'./installed-entry': { installedContext: () => null },
		'./installed-host': { installedLaunchArgs: () => [] },
		'./ipc': { registerIpc() {} },
		'./menu': { installMenu: (actions) => { menu = actions; } },
		'./navigation': { installPermissionPolicy() {} },
		'./listener': { installListenerHeader() {} },
		'./disclosure': { AGENT_ACCESS: {}, agentAccessDetail: () => '', DisclosureStore: { forProfile: () => ({}) }, ensureAgentAccessAcknowledged: acknowledge },
		'./postgres': { PostgresSupervisor, resolvePostgresBinaries: () => ({}) },
		'./settings': { plainCipher: {}, SettingsStore: class { async load() {} postgres() { return null; } } },
		'./tray': { installTray() {} },
		'./updater': { Updater },
		'./installer-updates': { InstallerUpdates: class {}, installerPlatform: () => null },
		'./window': { MainWindow, PARTITION: 'test' },
		'./worker': { WorkerSupervisor, resolveRuntime: () => ({ mode: 'repo' }) }
	};
	const module = { exports: {} };
	vm.runInNewContext(readFileSync(file, 'utf8'), {
		module, exports: module.exports, __dirname: path.dirname(file),
		require: (id) => (id in replacements ? replacements[id] : localRequire(id)),
		process: { ...process, env: {}, platform: 'darwin' }, console, setTimeout, clearTimeout, Error
	}, { filename: file });
	for (let i = 0; i < 20 && !(worker && worker.state.phase === 'ready'); i++) await tick();
	await tick();
	return { app, appEvents, pages, errors, get window() { return mainWindow; }, get worker() { return worker; }, get menu() { return menu; } };
}

test('reopening the window while the worker still runs goes straight back to the worker UI', async () => {
	const main = await launch();
	assert.deepEqual(main.pages, ['status', URL_READY], 'first launch: status page, then the worker once it is ready');
	// macOS: the window closed, the app and its worker kept running; the dock icon reopens it.
	main.pages.length = 0;
	main.window.browserWindow = null;
	main.appEvents.emit('activate');
	for (let i = 0; i < 5; i++) await tick();
	assert.deepEqual(main.pages, [URL_READY], 'no status page that would wait for a ready event that never comes');
});

test('a menu restart whose session stop fails reports it and returns to the running worker', async () => {
	const unhandled = [];
	const onUnhandled = (reason) => unhandled.push(reason);
	process.on('unhandledRejection', onUnhandled);
	try {
		const main = await launch({ stop: async () => { throw new Error('Worker did not finish stopping sessions'); } });
		main.pages.length = 0;
		main.menu.restartWorker();
		for (let i = 0; i < 10; i++) await tick();
		assert.deepEqual(main.pages, ['status', URL_READY]);
		assert.deepEqual(main.errors, [['Restart Worker', 'Worker did not finish stopping sessions']]);
		assert.equal(main.worker.state.phase, 'ready');
		assert.deepEqual(unhandled, []);
	} finally {
		process.off('unhandledRejection', onUnhandled);
	}
});

test('a startup failure is shown and quits instead of leaving a windowless process', async () => {
	const main = await launch({ acknowledge: async () => { throw new Error('EACCES: agent-access.json'); } });
	assert.deepEqual(main.errors, [['AI Shuffle could not start', 'EACCES: agent-access.json']]);
	assert.equal(main.app.quits, 1);
	assert.deepEqual(main.pages, []);
});

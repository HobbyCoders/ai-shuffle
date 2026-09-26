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
async function launch({ stop = async () => {}, acknowledge = async () => true, realUpdater = false, postgresStart = null, installed = null, registered = false, messageBox = async () => ({ response: 0 }) } = {}) {
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
		commandLine: { getSwitchValue: () => '' },
		requestSingleInstanceLock: () => true,
		on: (name, fn) => appEvents.on(name, fn),
		whenReady: async () => {},
		quits: 0,
		quit() { app.quits++; }, relaunch() {},
		protocol: [],
		setAsDefaultProtocolClient(...args) { app.protocol.push(['set', ...args]); return true; },
		isDefaultProtocolClient(...args) { app.protocol.push(['is', ...args]); return registered; },
		removeAsDefaultProtocolClient(...args) { app.protocol.push(['remove', ...args]); return true; }
	};
	const pages = [];
	const serviceWorkerClears = [];
	let mainWindow;
	class MainWindow {
		constructor() { mainWindow = this; this.browserWindow = null; this.page = null; this.workerUrl = null; this.sent = []; }
		get currentPage() { return this.page; }
		get currentWorkerUrl() { return this.page === null ? this.workerUrl : null; }
		async showPage(mode) { this.browserWindow = {}; this.page = mode; pages.push(mode); }
		async loadWorker(url) { this.browserWindow = {}; this.page = null; this.workerUrl = url; pages.push(url); }
		focus() { if (this.browserWindow) this.browserWindow.shown = true; }
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
		async stop(options = {}) {
			this.stops = [...(this.stops ?? []), options.force ? 'force' : 'graceful'];
			if (!options.force) await stop();
			this.state = { ...this.state, phase: 'stopped' }; this.emit('state', this.state);
		}
		tail() { return []; }
		async activeSessionCount() { return 0; }
		async clearConnectionFile() {}
		async updatesAllowed() { return true; }
	}
	class PostgresSupervisor extends EventEmitter {
		constructor() { super(); this.state = { phase: 'idle', port: null, error: null, logPath: '' }; }
		async start() { if (postgresStart) await postgresStart(); this.state = { ...this.state, phase: 'ready', port: 5432 }; return this.state; }
		async stop() { this.state = { ...this.state, phase: 'stopped' }; }
		connection() { return {}; }
		tail() { return []; }
	}
	class FakeUpdater {
		setPolicy() {} setBridge() {} useApplications() {} subscribe() { return () => {}; } onBeforeInstall() {} onInstallFailed() {} onConfirmInstall() {} start() {} stop() {}
		async check() {}
	}
	let updater;
	let menu;
	let tray;
	let ipc;
	const replacements = {
		electron: electronFake(app, errors, messageBox),
		'./close-dialog': { showCloseDialog: async () => true },
		'./managed-context': { managedContext: () => null },
		'./installed-entry': { installedContext: () => installed },
		'./installed-host': { installedLaunchArgs: () => [] },
		'./ipc': { registerIpc: (deps) => { ipc = deps; } },
		'./menu': { installMenu: (actions) => { menu = actions; } },
		'./navigation': { installPermissionPolicy() {}, forgetServiceWorkers: async () => { serviceWorkerClears.push(pages.length); } },
		'./listener': { installListenerHeader() {} },
		'./disclosure': { AGENT_ACCESS: {}, agentAccessDetail: () => '', DisclosureStore: { forProfile: () => ({}) }, ensureAgentAccessAcknowledged: acknowledge },
		'./postgres': { PostgresSupervisor, resolvePostgresBinaries: () => ({}) },
		'./settings': { plainCipher: {}, SettingsStore: class { async load() {} postgres() { return null; } } },
		'./tray': { installTray: (actions) => { tray = actions; } },
		'./updater': { Updater: class extends (realUpdater ? loadUpdater(electronFake(app, errors, messageBox)) : FakeUpdater) { constructor(...args) { super(...args); updater = this; } } },
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
	return { app, appEvents, pages, errors, serviceWorkerClears, get window() { return mainWindow; }, get worker() { return worker; }, get menu() { return menu; }, get tray() { return tray; }, get ipc() { return ipc; }, get updater() { return updater; } };
}

function electronFake(app, errors, messageBox) {
	return {
		app,
		dialog: { showErrorBox: (title, message) => errors.push([title, message]), showMessageBox: (...args) => messageBox(args.at(-1), args.length > 1 ? args[0] : null) },
		safeStorage: { isEncryptionAvailable: () => false },
		session: { fromPartition: () => ({ setUserAgent() {}, getUserAgent: () => 'test' }) },
		shell: { showItemInFolder() {}, openPath: async () => '' }
	};
}

/** The real dist/main/updater.js, with its timers inert. */
function loadUpdater(electron) {
	const module = { exports: {} };
	const inert = () => 0;
	vm.runInNewContext(readFileSync(path.join(desktopDir, 'dist/main/updater.js'), 'utf8'), {
		module, exports: module.exports, require: (id) => (id === 'electron' ? electron : createRequire(import.meta.url)(id)),
		process: { env: {} }, setTimeout: inert, clearTimeout: inert, setInterval: inert, clearInterval: inert, Date, Error
	});
	return module.exports.Updater;
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

test('R10-10: the app partition\'s service workers are cleared once at startup, before any page loads', async () => {
	const main = await launch();
	assert.deepEqual(main.serviceWorkerClears, [0]);
	assert.deepEqual(main.pages, ['status', URL_READY]);
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

test('Worker Status… while the worker runs offers a way back to the worker UI', async () => {
	const main = await launch();
	main.pages.length = 0;
	main.menu.openWorkerStatus();
	await tick();
	assert.deepEqual(main.pages, ['status'], 'the ready worker emits no state change that would leave this page');
	await main.ipc.worker.openUi();
	assert.deepEqual(main.pages, ['status', URL_READY]);
	// Only from the status page, and only while the worker is ready.
	await main.ipc.worker.openUi();
	main.worker.state = { ...main.worker.state, phase: 'stopped' };
	await main.window.showPage('status');
	await main.ipc.worker.openUi();
	assert.deepEqual(main.pages, ['status', URL_READY, 'status']);
});

/** A staged source update whose install step fails after the pre-install stop. */
function failingInstall(updater) {
	updater.useApplications({
		version: 'a'.repeat(40), pendingVersion: 'b'.repeat(40), options: { root: '/isolated/source' },
		check: async () => ({ kind: 'ready' }), download: async () => ({ kind: 'ready' }),
		prepareInstall: async () => { throw new Error('Could not stage the update'); }
	});
	assert.equal(updater.current.phase, 'ready');
}

test('a failed update install restarts the stack it stopped and reports why', async () => {
	const main = await launch({ realUpdater: true });
	failingInstall(main.updater);
	main.pages.length = 0;
	await main.updater.install();
	assert.deepEqual(main.pages, ['status', URL_READY], 'stopped for the install, then back to the worker UI');
	assert.equal(main.worker.state.phase, 'ready');
	assert.deepEqual(main.errors, [['Update could not be installed', 'Could not stage the update']]);
	assert.equal(main.updater.current.phase, 'ready', 'the staged update can be retried');
	assert.match(main.updater.current.error, /Could not stage the update/);
	assert.equal(main.app.quits, 0);
});

test('a failed restart after a failed update install is shown, not swallowed', async () => {
	let starts = 0;
	const main = await launch({ realUpdater: true, postgresStart: async () => { if (++starts > 1) throw new Error('pg_ctl: could not start server'); } });
	failingInstall(main.updater);
	main.pages.length = 0;
	await main.updater.install();
	assert.deepEqual(main.pages, ['status']);
	assert.equal(main.errors.length, 1);
	assert.equal(main.errors[0][0], 'AI Shuffle could not restart');
	assert.match(main.errors[0][1], /Could not stage the update/);
	assert.match(main.errors[0][1], /pg_ctl: could not start server/);
});

test('NC-11: the tray Quit ends a wedged worker after its graceful stop fails', async () => {
	const main = await launch({ stop: async () => { throw new Error('Worker did not finish stopping sessions'); } });
	main.tray.forceQuit();
	for (let i = 0; i < 20 && main.app.quits === 0; i++) await tick();
	assert.deepEqual(main.worker.stops, ['graceful', 'force'], 'graceful first, then the kill');
	assert.equal(main.app.quits, 1);
	// The app's own before-quit now lets it go.
	let prevented = false;
	main.appEvents.emit('before-quit', { preventDefault: () => { prevented = true; } });
	assert.equal(prevented, false);
});

test('NC-11: the tray Quit with a healthy worker is the ordinary graceful quit', async () => {
	const main = await launch();
	main.tray.forceQuit();
	for (let i = 0; i < 20 && main.app.quits === 0; i++) await tick();
	assert.deepEqual(main.worker.stops, ['graceful']);
	assert.equal(main.app.quits, 1);
});

test('NC-16: no aishuffle:// handler is registered, and one left by an older build is removed', async () => {
	const executable = '/Programs/AI Shuffle/AI Shuffle.exe';
	const stale = await launch({ installed: { executable }, registered: true });
	assert.deepEqual(stale.app.protocol, [['is', 'aishuffle', executable], ['remove', 'aishuffle', executable]]);
	const clean = await launch({ installed: { executable } });
	assert.deepEqual(clean.app.protocol, [['is', 'aishuffle', executable]]);
	const development = await launch();
	assert.deepEqual(development.app.protocol, [], 'an unpackaged run never touches the OS handler');
});

/** A staged source update that installs cleanly. */
function stagedInstall(updater) {
	updater.useApplications({
		version: 'a'.repeat(40), pendingVersion: 'b'.repeat(40), options: { root: '/isolated/source' },
		check: async () => ({ kind: 'ready' }), download: async () => ({ kind: 'ready' }),
		application: async () => ({ node: '/isolated/node', desktopDir: '/isolated/desktop' })
	});
}

test('restarting into an update asks first while sessions run, and a declined restart stops nothing', async () => {
	const asked = [];
	let response = 1;
	const main = await launch({ realUpdater: true, messageBox: async (options) => { asked.push(options); return { response }; } });
	stagedInstall(main.updater);
	main.worker.activeSessionCount = async () => 2;
	await main.updater.install();
	assert.equal(asked.length, 1);
	assert.match(asked[0].detail, /You have 2 active sessions\. Restarting stops any active sessions\./);
	assert.equal(asked[0].buttons[asked[0].cancelId], 'Cancel');
	assert.equal(asked[0].defaultId, asked[0].cancelId, 'Enter does not stop the sessions');
	assert.equal(main.worker.stops, undefined, 'declined: the worker keeps running');
	assert.equal(main.worker.state.phase, 'ready');
	assert.equal(main.app.quits, 0);
	// A session count that cannot be read asks too.
	main.worker.activeSessionCount = async () => { throw new Error('503'); };
	await main.updater.install();
	assert.match(asked[1].detail, /could not check for active sessions/);
	assert.equal(main.app.quits, 0);
	// Confirmed: the stack stops and the app restarts into the update.
	response = 0;
	main.worker.activeSessionCount = async () => 1;
	await main.updater.install();
	assert.match(asked[2].detail, /You have 1 active session\. Restarting stops it\./);
	assert.deepEqual(main.worker.stops, ['graceful']);
	assert.equal(main.app.quits, 1);
	// With no session running there is nothing to ask.
	const idle = await launch({ realUpdater: true, messageBox: async (options) => { asked.push(options); return { response: 1 }; } });
	stagedInstall(idle.updater);
	await idle.updater.install();
	assert.equal(asked.length, 3);
	assert.equal(idle.app.quits, 1);
});

test('nothing opens and no worker starts before the agent-access disclosure is accepted', async () => {
	let accept;
	const main = await launch({ acknowledge: () => new Promise((resolve) => { accept = resolve; }) });
	// A dock click or the tray's Open while the first-run disclosure is showing.
	main.appEvents.emit('activate');
	main.tray.show();
	for (let i = 0; i < 10; i++) await tick();
	assert.deepEqual(main.pages, []);
	assert.equal(main.worker, undefined, 'the runtime is not even resolved');
	accept(true);
	for (let i = 0; i < 20 && !(main.worker && main.worker.state.phase === 'ready'); i++) await tick();
	await tick();
	assert.deepEqual(main.pages, ['status', URL_READY]);
});

test('the first-run disclosure is owned by a shown main window, and nothing starts until it is accepted', async () => {
	let answer;
	const asked = [];
	const main = await launch({
		acknowledge: async (_store, ask) => (await ask()) === 'continue',
		messageBox: (options, parent) => { asked.push({ options, parent, shown: parent?.shown === true }); return new Promise((resolve) => { answer = resolve; }); }
	});
	// Windows applies an older launcher's SW_HIDE to the first window shown; a parentless dialog would stay invisible.
	assert.equal(asked.length, 1);
	assert.notEqual(asked[0].parent, null, 'the dialog has the main window as its owner');
	assert.equal(asked[0].shown, true, 'the main window was shown before the dialog');
	assert.deepEqual(main.pages, ['status']);
	// The tray's Open brings the window, and so the dialog, forward without starting anything.
	main.tray.show();
	for (let i = 0; i < 10; i++) await tick();
	assert.equal(main.worker, undefined);
	answer({ response: 0 });
	for (let i = 0; i < 20 && !(main.worker && main.worker.state.phase === 'ready'); i++) await tick();
	await tick();
	assert.equal(main.worker.state.phase, 'ready');
	assert.deepEqual(main.pages, ['status', URL_READY]);
});

test('quitting a first boot before its database started lets the next launch retry it; after that it does not', async () => {
	const applications = () => ({ options: { root: '/isolated/source' }, deferred: 0, async deferBoot() { this.deferred++; }, async databaseStarting() { this.starting = (this.starting ?? 0) + 1; }, async healthy() {}, async failed() {} });
	const executable = '/Programs/AI Shuffle/AI Shuffle.exe';
	// The tray's Quit while the first-run disclosure is still waiting.
	const waiting = applications();
	const early = await launch({ installed: { executable, updates: waiting }, acknowledge: () => new Promise(() => {}) });
	early.tray.forceQuit();
	for (let i = 0; i < 20 && early.app.quits === 0; i++) await tick();
	assert.equal(early.app.quits, 1);
	assert.equal(waiting.deferred, 1);
	// Once the stack started, a quit leaves the first boot to the launcher's crash check.
	const started = applications();
	const late = await launch({ installed: { executable, updates: started } });
	assert.equal(late.worker.state.phase, 'ready');
	late.tray.forceQuit();
	for (let i = 0; i < 20 && late.app.quits === 0; i++) await tick();
	assert.equal(late.app.quits, 1);
	assert.equal(started.deferred, 0);
	assert.equal(started.starting, 1, 'the launcher is told before the database starts');
	assert.equal(waiting.starting, undefined);
});

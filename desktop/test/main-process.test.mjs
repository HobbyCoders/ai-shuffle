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
async function launch({ stop = async () => {}, acknowledge = async () => true, realUpdater = false, postgresStart = null, installed = null, registered = false, messageBox = async () => ({ response: 0 }), installers = null, heldLock = false, managed = null, platform = 'darwin', env = {}, delegatedApplication = null } = {}) {
	const file = path.join(desktopDir, 'dist/main/index.js');
	const localRequire = createRequire(file);
	const appEvents = new EventEmitter();
	const errors = [];
	const splashes = [];
	const app = {
		// Linux delegation only ever runs inside the packaged app.
		isPackaged: !!installed || !!installers || !!delegatedApplication,
		getAppPath: () => desktopDir,
		getPath: () => path.join(desktopDir, 'no-user-data'),
		getVersion: () => '0.0.0-test',
		setAppUserModelId() {}, setName() {}, setPath() {},
		commandLine: { getSwitchValue: () => '' },
		hasSingleInstanceLock: () => heldLock,
		lockRequests: 0,
		requestSingleInstanceLock() { this.lockRequests++; return true; },
		on: (name, fn) => appEvents.on(name, fn),
		whenReady: async () => {},
		quits: 0,
		quit() { app.quits++; }, relaunch(options) { app.relaunched = options; },
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
		constructor(options) {
			super();
			this.options = options;
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
	let postgres;
	class PostgresSupervisor extends EventEmitter {
		constructor(options) { super(); postgres = this; this.options = options; this.state = { phase: 'idle', port: null, error: null, logPath: '' }; }
		async start() { if (postgresStart) await postgresStart(); this.state = { ...this.state, phase: 'ready', port: 5432 }; return this.state; }
		async stop() { this.state = { ...this.state, phase: 'stopped' }; }
		async quiesce() { await this.stop(); }
		updateRestorePoint() { return { validate: async () => { this.validations = (this.validations ?? 0) + 1; } }; }
		connection() { return {}; }
		tail() { return []; }
	}
	class FakeUpdater {
		get current() { return { release: '1.0.2' }; }
		setPolicy() {} setBridge() {} useApplications() {} subscribe() { return () => {}; } onBeforeInstall() {} onInstallFailed(fn) { this.installFailed = fn; } onInstallGuard() {} onRestarting(fn) { this.restarting = fn; } onRestartCancelled(fn) { this.restartCancelled = fn; } onFailedStart(recovery) { this.failedStart = recovery; } onInstallPrepared(prepare, discard) { this.prepared = { prepare, discard }; } start() {} stop() {}
		async check() {}
	}
	let updater;
	let menu;
	let tray;
	let trayName;
	let ipc;
	const replacements = {
		electron: electronFake(app, errors, messageBox),
		'./close-dialog': { showCloseDialog: async () => true },
		'./call-popout': { CallPopout: class { register() {} } },
		'./managed-context': { managedContext: () => managed, setManagedContext: context => { managed = context; } },
		'./update-splash': { launcherImage: localRequire('./update-splash').launcherImage, launchUpdateSplash: options => { const splash = { options, closed: false }; splashes.push(splash); return () => { splash.closed = true; }; } },
		'./installed-entry': { installedContext: () => installed },
		'./installed-host': { installedLaunchArgs: () => [], launcherVersion: localRequire('./installed-host').launcherVersion, LAUNCHER_VERSION_ENV: 'SHUFFLE_LAUNCHER_VERSION' },
		'./ipc': { registerIpc: (deps) => { ipc = deps; } },
		'./menu': { installMenu: (actions) => { menu = actions; } },
		'./navigation': { installPermissionPolicy() {}, forgetServiceWorkers: async () => { serviceWorkerClears.push(pages.length); } },
		'./listener': { installListenerHeader() {} },
		'./disclosure': { AGENT_ACCESS: {}, agentAccessDetail: () => '', DisclosureStore: { forProfile: () => ({}) }, ensureAgentAccessAcknowledged: acknowledge },
		'./postgres': { PostgresSupervisor, resolvePostgresBinaries: () => ({}) },
		'./settings': { plainCipher: {}, SettingsStore: class { async load() {} postgres() { return null; } } },
		'./tray': { installTray: (actions, name) => { tray = actions; trayName = name; } },
		'./updater': { Updater: class extends (realUpdater ? loadUpdater(electronFake(app, errors, messageBox)) : FakeUpdater) { constructor(...args) { super(...args); updater = this; } } },
		'./installer-updates': { InstallerUpdates: installers ?? class {}, installerPlatform: () => (installers ? (platform==='linux'?'linux':'mac') : null) },
		'./window': { MainWindow, PARTITION: 'test' },
		'./worker': { WorkerSupervisor, resolveRuntime: () => ({ mode: 'repo' }) }
	};
	const module = { exports: {} };
	const sandbox = {
		module, exports: module.exports, __dirname: path.dirname(file),
		require: (id) => (id in replacements ? replacements[id] : localRequire(id)),
		process: { ...process, env, platform }, console, setTimeout, clearTimeout, Error
	};
	const runIndex = () => vm.runInNewContext(readFileSync(file, 'utf8'), sandbox, { filename: file });
	if (delegatedApplication) {
		const buildModule = { exports: {} };
		vm.runInNewContext(readFileSync(path.join(desktopDir, 'dist/main/source-build.js'), 'utf8'), {
			...sandbox, module: buildModule, exports: buildModule.exports, require: localRequire
		});
		const entryModule = { exports: {} };
		const entryModules = {
			'./source-build': buildModule.exports,
			'./source-updates': { SourceUpdates: class {
				constructor(options) { this.options = options; }
				async attach() {} async application() { return delegatedApplication; } async requireRestorePoint() {} async databaseStarting() {} async healthy() {} async finishRollback() {}
			} },
			'./source-profile': { readProfile: () => null, localFeed: () => undefined },
			'./installed-host': { readInstalledHostSync: () => null },
			'./index': null
		};
		vm.runInNewContext(readFileSync(path.join(desktopDir, 'dist/main/managed-entry.js'), 'utf8'), {
			...sandbox, module: entryModule, exports: entryModule.exports,
			require: id => id === './index' ? runIndex() : id in entryModules ? entryModules[id] : sandbox.require(id)
		});
		await entryModule.exports.startManagedSource(path.join(desktopDir, 'no-user-data/updates'), 'a'.repeat(40), delegatedApplication.node);
	} else runIndex();
	for (let i = 0; i < 20 && !(worker && worker.state.phase === 'ready'); i++) await tick();
	await tick();
	return { app, appEvents, pages, errors, splashes, serviceWorkerClears, get postgres() { return postgres; }, get window() { return mainWindow; }, get worker() { return worker; }, get menu() { return menu; }, get tray() { return tray; }, get trayName() { return trayName; }, get ipc() { return ipc; }, get updater() { return updater; } };
}

test('Windows source restart shows the release window and a failed relaunch closes it', async () => {
  const revision = 'b'.repeat(40);
  const updates = { options: { root: '/isolated/no-root' }, pendingVersion: revision, async requireRestorePoint() {}, async databaseStarting() {}, async healthy() {}, async finishRollback() {}, async failed() {} };
  const managed = { root: '/isolated/no-root', updates, application: { revision: 'a'.repeat(40), resources: '/isolated/resources', node: process.execPath, serverDir: '/isolated/server', desktopDir: '/isolated/desktop', python: '/isolated/python' }, host: { executable: 'C:/AI Shuffle.exe' } };
  const f = await launch({ platform: 'win32', managed });
  f.updater.restarting();
  assert.equal(f.splashes.length, 1);
  assert.equal(f.splashes[0].options.release, '1.0.2');
  assert.equal(f.splashes[0].options.revision, revision);
  assert.equal(f.splashes[0].options.launcher, 'AI Shuffle.exe');
  assert.equal(f.app.notifications.length, 1, 'a spawned child is not proof the notice rendered');
  f.updater.restartCancelled();
  await f.updater.installFailed(Error('relaunch failed'));
  assert.equal(f.splashes[0].closed, true);
});

test('a source update starts its worker with its verified release version; the built-in build and an unreadable release set none (#365)', async () => {
  const R = 'b'.repeat(40);
  const application = { revision: R, resources: '/isolated/resources', node: process.execPath, serverDir: '/isolated/server', desktopDir: '/isolated/desktop', python: '/isolated/python' };
  const requested = [];
  const updates = (release) => ({ options: { root: '/isolated/no-root' }, release, async requireRestorePoint() {}, async databaseStarting() {}, async healthy() {}, async finishRollback() {}, async failed() {} });
  const source = await launch({ platform: 'win32', managed: { root: '/isolated/no-root', application, host: { executable: 'C:/AI Shuffle.exe' },
    updates: updates(async (revision) => { requested.push(revision); return { format: 2, revision, sequence: 337, version: '1.0.2-dev.337+ga48a61d' }; }) } });
  assert.equal(source.worker.state.phase, 'ready');
  assert.equal(source.worker.options.appVersion, '1.0.2-dev.337+ga48a61d');
  assert.ok(requested.length && requested.every(revision => revision === R), 'the version is read from the running revision\'s signed release');
  // release() verifies release.json again; a failure starts the worker on the fallback version.
  const unverified = await launch({ platform: 'win32', managed: { root: '/isolated/no-root', application, updates: updates(async () => { throw Error('Invalid source manifest signature'); }) } });
  assert.equal(unverified.worker.state.phase, 'ready');
  assert.equal('appVersion' in unverified.worker.options, false);
  const builtIn = await launch({ platform: 'win32' });
  assert.equal('appVersion' in builtIn.worker.options, false);
});

for (const launcher of [undefined, '3', 'unknown', '4', '5', '6', '7', '8']) test(`main process keeps full validation with launcher ${launcher} when no installed handoff is known`, async () => {
  const updates = { options: { root: '/isolated/no-root' }, hasMigrationRisk: false, async requireRestorePoint(database) { await database.validate(); }, async databaseStarting() {}, async healthy() {}, async finishRollback() {}, async failed() {} };
  const f = await launch({ platform: 'win32', managed: { root: '/isolated/no-root', updates, application: { resources: '/isolated/resources', node: process.execPath, serverDir: '/isolated/server', desktopDir: '/isolated/desktop', python: '/isolated/python' } }, env: { SHUFFLE_LAUNCHER_VERSION: launcher } });
  assert.deepEqual(f.errors, []);
  assert.equal(f.postgres.validations, 1);
});

test('direct source relaunch clears the previous launcher handoff proof', async () => {
  const env = { SHUFFLE_LAUNCHER_VERSION: '4' };
  const updates = { options: { root: '/isolated/no-root' }, hasMigrationRisk: false,
    async requireRestorePoint() { throw Error('journal read failed'); }, async failed() {} };
  const managed = { root: '/isolated/no-root', updates, host: { executable: 'C:/AI Shuffle.exe' },
    application: { resources: '/isolated/resources', node: process.execPath, serverDir: '/isolated/server', desktopDir: '/isolated/desktop', python: '/isolated/python' } };
  const f = await launch({ platform: 'win32', managed, env });
  let relaunched = false;
  f.app.relaunch = () => { relaunched = true; assert.equal(env.SHUFFLE_LAUNCHER_VERSION, undefined); };
  await f.ipc.worker.restart();
  assert.equal(relaunched, true);
  assert.equal(f.app.quits, 1);
});

for (const launcher of ['9', '999']) test(`installed launcher ${launcher} keeps the full hash and does not pre-create a point`, async () => {
  const updates = { options: { root: '/isolated/no-root' }, hasMigrationRisk: false,
    async requireRestorePoint(database) { await database.validate(); }, async databaseStarting() {}, async healthy() {}, async finishRollback() {}, async failed() {} };
  const managed = { root: '/isolated/no-root', updates, host: { executable: 'C:/AI Shuffle.exe' },
    application: { resources: '/isolated/resources', node: process.execPath, serverDir: '/isolated/server', desktopDir: '/isolated/desktop', python: '/isolated/python' } };
  const f = await launch({ platform: 'win32', managed, env: { SHUFFLE_LAUNCHER_VERSION: launcher } });
  assert.deepEqual(f.errors, []);
  assert.equal(f.postgres.validations, 1);
  assert.equal(f.updater.prepared, undefined);
});

function electronFake(app, errors, messageBox) {
	return {
		app,
		Notification: class {
			static isSupported() { return true; }
			constructor(options) { this.options = options; }
			show() { (app.notifications ??= []).push(this.options); }
		},
		dialog: { showErrorBox: (title, message) => errors.push([title, message]), showMessageBox: (...args) => messageBox(args.at(-1), args.length > 1 ? args[0] : null) },
		safeStorage: { isEncryptionAvailable: () => false },
		session: { fromPartition: () => ({ setUserAgent() {}, getUserAgent: () => 'test' }) },
		shell: { showItemInFolder(file) { app.revealed = [...(app.revealed ?? []), file]; }, openPath: async () => '' }
	};
}

/** The real dist/main/updater.js, with its timers inert. */
function loadUpdater(electron) {
	const module = { exports: {} };
	const inert = () => 0;
	vm.runInNewContext(readFileSync(path.join(desktopDir, 'dist/main/updater.js'), 'utf8'), {
		// Its own imports (./channel-return) resolve beside it, as in the packaged app.
		module, exports: module.exports, require: (id) => (id === 'electron' ? electron : createRequire(path.join(desktopDir, 'dist/main/updater.js'))(id)),
		process: { env: {} }, setTimeout: inert, clearTimeout: inert, setInterval: inert, clearInterval: inert, Date, Error, console
	});
	return module.exports.Updater;
}

test('in-process source delegation inherits the installed lock without requesting or releasing it',async()=>{
 const main=await launch({heldLock:true});assert.equal(main.app.lockRequests,0);assert.equal(main.app.quits,0);assert.equal(main.worker.state.phase,'ready');
});
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

test("the menu's Check for Updates shows the window on the in-app Updates screen and runs no native check", async () => {
	const asked = [];
	const main = await launch({ messageBox: async (options) => { asked.push(options); return { response: 1 }; } });
	assert.equal(main.menu.checkForUpdates, undefined, 'the native dialog check is gone');
	assert.equal(main.menu.updatesEnabled, true, 'stable builds still show the item');
	main.window.sent.length = 0;
	main.menu.openUpdates();
	for (let i = 0; i < 5; i++) await tick();
	assert.equal(main.window.browserWindow.shown, true);
	assert.deepEqual(main.window.sent, [['desktop:command', 'open-updates']]);
	assert.deepEqual(asked, []);
	// The tray shows the product name, not the package name app.name reports.
	assert.equal(main.trayName, 'AI Shuffle');
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
	assert.deepEqual(main.errors, [], 'the worker UI is back, so its update button and bell show the failure');
	assert.equal(main.updater.current.phase, 'ready', 'the staged update can be retried');
	assert.match(main.updater.current.error, /Could not stage the update/);
	assert.equal(main.app.quits, 0);
});

test('a failed install whose worker does not come back is shown natively: there is no web UI to show it', async () => {
	const main = await launch({ realUpdater: true });
	failingInstall(main.updater);
	main.worker.start = async function () { this.state = { ...this.state, phase: 'failed', error: 'port in use' }; this.emit('state', this.state); return this.state; };
	await main.updater.install();
	assert.deepEqual(main.errors, [['Update could not be installed', 'Could not stage the update']]);
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

test('restarting into an update waits for running sessions without a native prompt, and reports why in the update state', async () => {
	const asked = [];
	const main = await launch({ realUpdater: true, messageBox: async (options) => { asked.push(options); return { response: 0 }; } });
	stagedInstall(main.updater);
	main.worker.activeSessionCount = async () => 2;
	await main.updater.install();
	assert.deepEqual(asked, [], 'no native confirmation');
	assert.equal(main.worker.stops, undefined, 'sessions keep running');
	assert.equal(main.worker.state.phase, 'ready');
	assert.equal(main.app.quits, 0);
	assert.equal(main.updater.current.phase, 'ready', 'still staged for a retry from the update button');
	assert.equal(main.updater.current.error, 'Busy with active sessions; retry when finished.');
	// A session count that cannot be read never restarts either.
	main.worker.activeSessionCount = async () => { throw new Error('503'); };
	await main.updater.install();
	assert.match(main.updater.current.error, /Could not check for active sessions/);
	assert.equal(main.app.quits, 0);
	// With no session running the stack stops and the app restarts into the update.
	main.worker.activeSessionCount = async () => 0;
	await main.updater.install();
	assert.deepEqual(main.worker.stops, ['graceful']);
	assert.equal(main.app.quits, 1);
	assert.deepEqual(asked, []);
});

test('Linux in-process delegation produces scrubbed worker/PostgreSQL environments but keeps the AppImage host environment',async()=>{
 const env={PATH:'/usr/bin',HOME:'/home/owner',APPIMAGE:'/downloads/AI Shuffle.AppImage',APPDIR:'/tmp/.mount-old',LD_LIBRARY_PATH:'/tmp/.mount-old/usr/lib',LD_PRELOAD:'/tmp/.mount-old/preload.so',DISPLAY:':7',WAYLAND_DISPLAY:'wayland-0',XDG_RUNTIME_DIR:'/run/user/1000',DBUS_SESSION_BUS_ADDRESS:'unix:path=/run/user/1000/bus',XAUTHORITY:'/home/owner/.Xauthority',CLAUDE_CONFIG_DIR:'/home/owner/.claude',SHUFFLE_UPDATE_POLICY_FILE:'/wrong/inherited-policy.json'};
 const application={resources:path.join(desktopDir,'resources'),python:'/staged/python/bin/python3',serverDir:'/staged/server',node:'/staged/node/bin/node',desktopDir};
 const main=await launch({heldLock:true,platform:'linux',env,delegatedApplication:application});
 assert.equal(main.worker.state.phase,'ready');assert.deepEqual(main.errors,[]);
 const {buildWorkerEnv}=createRequire(import.meta.url)('../dist/main/worker.js');
 const workerEnv=buildWorkerEnv({...main.worker.options,platform:'linux'});
 assert.equal(workerEnv.SHUFFLE_UPDATE_POLICY_FILE,path.join(desktopDir,'no-user-data/updates','allow-updates.json'));
 assert.equal(main.worker.options.baseEnv.SHUFFLE_UPDATE_POLICY_FILE,undefined,'do not broadly restore inherited variables');
 assert.equal(env.SHUFFLE_UPDATE_POLICY_FILE,'/wrong/inherited-policy.json','the policy path is injected at worker spawn, not into global env');
 let pgEnv;const {PostgresSupervisor:RealPostgres}=createRequire(import.meta.url)('../dist/main/postgres.js');
 const postgres=new RealPostgres({...main.postgres.options,runtime:{...main.postgres.options.runtime,exec:async(_file,_args,options)=>{pgEnv=options.env;return {code:0,stdout:'',stderr:''};}}});
 // Exercise the real PostgreSQL command-environment consumer, intercepting only spawn.
 await postgres.exec('/staged/postgres/bin/pg_ctl',['--version']);
 for(const childEnv of [workerEnv,pgEnv]){
  for(const key of ['APPIMAGE','APPDIR','LD_LIBRARY_PATH','LD_PRELOAD'])assert.equal(childEnv[key],undefined,key+' must not leak to managed runtimes');
  for(const key of ['DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','XAUTHORITY','HOME','CLAUDE_CONFIG_DIR'])assert.equal(childEnv[key],env[key],key+' must survive');
 }
 assert.equal(env.APPIMAGE,'/downloads/AI Shuffle.AppImage');assert.equal(env.LD_LIBRARY_PATH,'/tmp/.mount-old/usr/lib','never scrub the running host in place');
});
test('Linux installer-only baseline context suppresses the incompatible staged source offer on subsequent launches',async()=>{
 class Installers{async check(){return {kind:'available',version:'0.9.9',release:'0.9.9',installer:{}};}}
 const updates=new Proxy({},{get(){throw Error('Must use installer updates');}});
 const main=await launch({realUpdater:true,installers:Installers,heldLock:true,platform:'linux',installed:{updates,root:'/installed/application-updates',executable:'/downloads/AI Shuffle.AppImage',installersOnly:true}});
 assert.equal(main.worker.options.updatePolicyFile,path.join('/installed/application-updates','allow-updates.json'));
 await main.updater.check();assert.equal(main.updater.current.phase,'available');assert.equal(main.updater.current.release,'0.9.9');assert.deepEqual(main.errors,[]);
});
test('Linux fallback with an active source keeps that worker but offers installers, never repeats incompatible source activation',async()=>{
 class Installers{async check(){return {kind:'available',version:'0.9.9',release:'0.9.9',installer:{}};}}
 const application={resources:path.join(desktopDir,'resources'),python:'/staged/bin/python',serverDir:'/staged/server',node:'/staged/bin/node',desktopDir};
 const updates=new Proxy({},{get(){throw Error('Must use installer updates');}});
 const main=await launch({realUpdater:true,installers:Installers,heldLock:true,platform:'linux',managed:{application,updates,root:'/active/application-updates',installersOnly:true}});
 assert.equal(main.worker.options.updatePolicyFile,path.join('/active/application-updates','allow-updates.json'));
 await main.updater.check();assert.equal(main.worker.state.phase,'ready');assert.equal(main.updater.current.phase,'available');assert.equal(main.updater.current.release,'0.9.9');assert.deepEqual(main.errors,[]);assert.equal(main.app.lockRequests,0);
});
test('packaged macOS/Linux: a found installer is offered only in the web UI, and a download is revealed and announced', async () => {
	const asked = [];
	class Installers {
		async check() { return { kind: 'available', version: '0.9.0', release: '0.9.0', installer: {} }; }
		async download() { return { file: '/Users/me/Downloads/AI-Shuffle-0.9.0.dmg', version: '0.9.0' }; }
	}
	const main = await launch({ realUpdater: true, installers: Installers, messageBox: async (options) => { asked.push(options); return { response: 0 }; } });
	await main.updater.check();
	assert.equal(main.updater.current.phase, 'available');
	assert.deepEqual(asked, [], 'no native "is available" offer');
	const sent = () => main.window.sent.filter(([channel]) => channel === 'desktop:update-state').map(([, state]) => state);
	assert.equal(sent().at(-1).phase, 'available', 'the web UI learns of the release');
	await main.updater.download();
	assert.deepEqual(main.app.revealed, ['/Users/me/Downloads/AI-Shuffle-0.9.0.dmg']);
	assert.equal(sent().at(-1).installer, '/Users/me/Downloads/AI-Shuffle-0.9.0.dmg', 'the web UI can say where it went');
	assert.deepEqual(asked, []);
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

test('first-start failure reaps the stack and relaunches the installed selector, not the failed source Electron',async()=>{
 const events=[];
 const updates={options:{root:'/isolated/source'},async requireRestorePoint(){events.push('validate');},async databaseStarting(){events.push('floor');},async healthy(){},async finishRollback(){},async recoverRollback(){assert.fail('healthy callback must never restore');},async failed(){events.push('failed');},async rollbackVersion(){return '1.0.0';}};
 const executable=path.resolve('Programs/AI Shuffle.exe');
 const main=await launch({installed:{executable,updates},platform:'win32',postgresStart:async()=>events.push('postgres')});
 assert.deepEqual(events,['validate','floor','postgres']);
 const stop=main.worker.stop.bind(main.worker);main.worker.stop=async options=>{events.push('worker-stop');await stop(options);};
 main.postgres.quiesce=async()=>{events.push('postgres-stop');};
 main.worker.emit('state',{...main.worker.state,phase:'failed'});
 for(let i=0;i<20&&!main.app.quits;i++)await tick();
 assert.deepEqual(events.slice(3),['failed','worker-stop','postgres-stop']);
 assert.equal(main.app.relaunched.execPath,executable);assert.equal(main.app.quits,1);
});
test('missing restore point fails closed before PostgreSQL or worker start',async()=>{
 const updates={options:{root:'/isolated/source'},async requireRestorePoint(){throw Error('No verified database restore point');},async databaseStarting(){assert.fail('must not lower the fence');}};
 const main=await launch({installed:{executable:path.resolve('Programs/AI Shuffle.exe'),updates}});
 assert.equal(main.postgres.state.phase,'idle');assert.equal(main.worker.state.phase,'idle');
});
test('an older launcher\'s update copies the database for its restore point before PostgreSQL starts, and the window says so',async()=>{
 const events=[];
 const updates={options:{root:'/isolated/source'},async requireRestorePoint(validator,late){
  events.push('validate');assert.equal(typeof validator.validate,'function');
  // The copy is the app's own PostgreSQL restore point, quiesced by the same supervisor.
  late.preparing();assert.equal(typeof (await late.database('a'.repeat(40))).validate,'function');events.push('late copy');
 },async databaseStarting(){events.push('floor');},async healthy(){},async finishRollback(){},async failed(){}};
 const main=await launch({installed:{executable:path.resolve('Programs/AI Shuffle.exe'),updates},postgresStart:async()=>events.push('postgres')});
 assert.deepEqual(events,['validate','late copy','floor','postgres']);
 const states=main.window.sent.filter(([channel])=>channel==='desktop:worker-state').map(([,state])=>state.startupStep);
 assert.ok(states.includes('restore-point'),'the status page is told about the copy');
 assert.equal(main.window.sent.filter(([channel])=>channel==='desktop:worker-state').at(-1)[1].startupStep,undefined);
});
test('a failed late restore point stops before PostgreSQL, shows its plain reason, and Try again repeats it in this process',async()=>{
 const {RestorePointError}=createRequire(import.meta.url)('../dist/main/source-updates.js');
 const message='Could not prepare the update\'s database restore point: disk full. There is not enough free disk space for a copy of your database.';
 let attempts=0;const events=[];
 const updates={options:{root:'/isolated/source'},async requireRestorePoint(_,late){if(++attempts===1)throw new RestorePointError(message);late.preparing();events.push('copied');},
  async databaseStarting(){events.push('floor');},async healthy(){},async finishRollback(){},async failed(){}};
 const main=await launch({installed:{executable:path.resolve('Programs/AI Shuffle.exe'),updates},postgresStart:async()=>events.push('postgres')});
 assert.equal(main.postgres.state.phase,'idle');assert.deepEqual(events,[]);
 const state=main.window.sent.filter(([channel])=>channel==='desktop:worker-state').at(-1)[1];
 assert.equal(state.phase,'failed');assert.equal(state.error,message);assert.equal(state.startupStep,'restore-point');
 // A relaunch would go through an old launcher, which runs the previous version instead.
 const before=main.window.sent.length;
 await main.ipc.worker.restart();
 assert.equal(main.app.relaunched,undefined);assert.equal(main.app.quits,0);
 assert.deepEqual(events,['copied','floor','postgres']);assert.equal(main.worker.state.phase,'ready');
 // While Try again waits for the copy, the status page is told it is copying, not that it failed.
 const retried=main.window.sent.slice(before).filter(([channel])=>channel==='desktop:worker-state').map(([,state])=>state);
 assert.ok(retried.some(state=>state.startupStep==='restore-point'&&state.phase!=='failed'),JSON.stringify(retried.map(state=>[state.phase,state.startupStep])));
});
test('quitting a first boot before its database started lets the next launch retry it; after that it does not', async () => {
	const applications = () => ({ options: { root: '/isolated/source' }, deferred: 0, async requireRestorePoint() {}, async finishRollback() {}, async deferBoot() { this.deferred++; }, async databaseStarting() { this.starting = (this.starting ?? 0) + 1; }, async healthy() {}, async failed() {} });
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

test('an update\'s first start gives its worker 10 minutes to become ready; other starts keep the usual limit', async () => {
	const { promises: fs } = await import('node:fs');
	const os = await import('node:os');
	const R = 'b'.repeat(40);
	for (const [booting, expected] of [[R, 10 * 60_000], [undefined, undefined]]) {
		const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-first-start-'));
		try {
			await fs.writeFile(path.join(root, 'state.json'), JSON.stringify({ schemaVersion: 1, active: 'a'.repeat(40), ...(booting ? { pending: R, booting, waiting: R } : {}) }, null, 2) + '\n');
			const updates = { options: { root }, async requireRestorePoint() {}, async databaseStarting() {}, async healthy() {}, async finishRollback() {}, async failed() {}, async load() {} };
			const application = { revision: R, resources: '/isolated/resources', python: '/isolated/python', serverDir: '/isolated/server', node: '/isolated/node', desktopDir: '/isolated/desktop' };
			const main = await launch({ managed: { root, application, updates, host: null } });
			assert.equal(main.worker.options.readyTimeoutMs, expected, booting ? 'first start' : 'ordinary start');
			// The previous version's updater can offer a put-back first start again.
			assert.equal(typeof main.updater.failedStart?.release, 'function');
			assert.equal(typeof main.updater.restarting, 'function');
		} finally { await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
	}
});

for (const launcher of ['4', '5', '6', '7', '8']) test(`restarting with launcher ${launcher} copies the database for its restore point with the window saying so`, async () => {
	const R = 'b'.repeat(40);
	const application = { revision: R, resources: '/isolated/resources', python: '/isolated/python', serverDir: '/isolated/server', node: '/isolated/node', desktopDir: '/isolated/desktop' };
	const host = { executable: 'C:/Programs/AI Shuffle/AI Shuffle.exe', userData: 'C:/Users/owner/AppData/Roaming/AI Shuffle', name: 'AI Shuffle' };
	const updates = (events) => ({ options: { root: '/isolated/no-root' }, async requireRestorePoint() {}, async databaseStarting() {}, healthy: () => new Promise(() => {}), async finishRollback() {}, async failed() {}, async load() {},
		async prepareRollback(factory) { const copy = await factory('a'.repeat(40)); events.push(['copy', copy.constructor.name]); },
		async rollbackVersion() { return events.ready ? '1.0.1' : null; }, async rollbackNotice() { return events.notice ?? null; },
		async cancelPreparedRollback() { events.push(['discard']); } });
	const events = []; events.ready = true;
	const main = await launch({ managed: { root: '/isolated/no-root', application, updates: updates(events), host }, platform: 'win32', env: { SHUFFLE_LAUNCHER_VERSION: launcher } });
	assert.deepEqual(main.errors, []);
	const before = main.window.sent.length;
	await main.updater.prepared.prepare();
	assert.deepEqual([...events], [['copy', 'IncrementalClusterCopy']]);
	assert.equal(main.window.currentPage, 'status');
	const steps = main.window.sent.slice(before).filter(([channel]) => channel === 'desktop:worker-state').map(([, state]) => state.startupStep);
	assert.deepEqual(steps, ['restore-point', undefined], 'the status page says the restore point is being prepared, then stops saying so');
	await main.updater.prepared.discard();
	assert.deepEqual(events.at(-1), ['discard']);
	// A copy that failed ends the install with the reason the launcher would have shown.
	const failing = []; failing.notice = 'Update 1.0.2 was not started because its database restore point failed: disk full';
	const failed = await launch({ managed: { root: '/isolated/no-root', application, updates: updates(failing), host }, platform: 'win32', env: { SHUFFLE_LAUNCHER_VERSION: launcher } });
	await assert.rejects(failed.updater.prepared.prepare(), /disk full/);
	// Launchers before restore points, isolated profiles and other platforms keep the launcher's own behaviour.
	for (const options of [{ env: { SHUFFLE_LAUNCHER_VERSION: '3' }, platform: 'win32', host }, { env: {}, platform: 'win32', host }, { env: { SHUFFLE_LAUNCHER_VERSION: '4' }, platform: 'linux', host }, { env: { SHUFFLE_LAUNCHER_VERSION: '4' }, platform: 'win32', host: null }]) {
		const other = await launch({ managed: { root: '/isolated/no-root', application, updates: updates([]), host: options.host }, platform: options.platform, env: options.env });
		assert.equal(other.updater.prepared, undefined, JSON.stringify(options));
	}
});
test('a fresh Windows packaged install prepares its first update restore point in the app', async () => {
  const events = [];
  const root = '/isolated/no-root';
  const updates = { options: { root }, async requireRestorePoint() {}, async databaseStarting() {},
    healthy: () => new Promise(() => {}), async finishRollback() {}, async failed() {},
    async prepareRollback(factory) { const copy = await factory('a'.repeat(40)); events.push(copy.constructor.name); },
    async rollbackVersion() { return '1.0.1'; }, async cancelPreparedRollback() { events.push('discard'); } };
  // No managed source context or inherited launcher version: this is the installer's bundled app.
  const main = await launch({ platform: 'win32', installed: { root, updates, executable: 'C:/Programs/AI Shuffle/AI Shuffle.exe' } });
  assert.deepEqual(main.errors, []);
  assert.equal(main.app.isPackaged, true);
  assert.equal(typeof main.updater.prepared?.prepare, 'function', 'the installer keeps the in-app copy before quitting');
  const before = main.window.sent.length;
  await main.updater.prepared.prepare();
  assert.deepEqual(events, ['IncrementalClusterCopy']);
  assert.equal(main.window.currentPage, 'status');
  const steps = main.window.sent.slice(before).filter(([channel]) => channel === 'desktop:worker-state').map(([, state]) => state.startupStep);
  assert.deepEqual(steps, ['restore-point', undefined]);
  await main.updater.prepared.discard();
  assert.equal(events.at(-1), 'discard');
});

test('while a restore point is ready for an update that has not started, nothing in this version reopens the database', async () => {
	const { promises: fs } = await import('node:fs');
	const os = await import('node:os');
	const A = 'a'.repeat(40), R = 'b'.repeat(40);
	const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-held-point-'));
	try {
		const starts = [];
		const updates = { options: { root }, version: A, async requireRestorePoint() {}, async databaseStarting() { starts.push('floor'); }, healthy: () => new Promise(() => {}), async finishRollback() {}, async failed() {}, async load() {} };
		const main = await launch({ installed: { executable: path.resolve('Programs/AI Shuffle.exe'), updates, root }, postgresStart: async () => starts.push('postgres') });
		assert.deepEqual(starts, ['floor', 'postgres']);
		// The copy for R was made, then the restart failed (or the quit was cancelled).
		await fs.writeFile(path.join(root, 'restore-point.json'), JSON.stringify({ schemaVersion: 1, phase: 'ready', revision: R, previous: A }));
		await main.postgres.stop();
		// Route 1: the failed install starts the stack again.
		await main.updater.installFailed(new Error('cannot relaunch'));
		// Route 2: Restart Worker.
		await main.ipc.worker.restart();
		assert.deepEqual(starts, ['floor', 'postgres'], 'the database stayed closed');
		const state = main.window.sent.filter(([channel]) => channel === 'desktop:worker-state').at(-1)[1];
		assert.equal(state.phase, 'failed'); assert.match(state.error, /Quit AI Shuffle and open it again to finish installing the update/);
		// A point for this version itself (its own first start), a committed one, or none at all does not hold it.
		for (const journal of [{ phase: 'ready', revision: A }, { phase: 'committed', revision: R }, null]) {
			if (journal) await fs.writeFile(path.join(root, 'restore-point.json'), JSON.stringify({ schemaVersion: 1, previous: 'c'.repeat(40), ...journal }));
			else await fs.rm(path.join(root, 'restore-point.json'), { force: true });
			const before = starts.length;
			await main.ipc.worker.restart();
			assert.equal(starts.length, before + 2, JSON.stringify(journal));
		}
		// Route 3: a launch (or reopening the window) while the point is held.
		await fs.writeFile(path.join(root, 'restore-point.json'), '{ not json');
		const held = [];
		await launch({ installed: { executable: path.resolve('Programs/AI Shuffle.exe'), updates: { ...updates, async databaseStarting() { held.push('floor'); } }, root }, postgresStart: async () => held.push('postgres') });
		assert.deepEqual(held, [], 'an unreadable journal holds the database closed too');
	} finally { await fs.rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
});
test('a restore point that cannot be discarded after a failed restart makes the status page say to quit and open the app again', async () => {
	const R = 'b'.repeat(40);
	const application = { revision: R, resources: '/isolated/resources', python: '/isolated/python', serverDir: '/isolated/server', node: '/isolated/node', desktopDir: '/isolated/desktop' };
	const host = { executable: 'C:/Programs/AI Shuffle/AI Shuffle.exe', userData: 'C:/Users/owner/AppData/Roaming/AI Shuffle', name: 'AI Shuffle' };
	const updates = { options: { root: '/isolated/no-root' }, async requireRestorePoint() {}, async databaseStarting() {}, healthy: () => new Promise(() => {}), async finishRollback() {}, async failed() {}, async load() {},
		async prepareRollback() {}, async rollbackVersion() { return '1.0.1'; }, async rollbackNotice() { return null; },
		async cancelPreparedRollback() { throw new Error('EBUSY: restore-point.json is locked'); } };
	const main = await launch({ managed: { root: '/isolated/no-root', application, updates, host }, platform: 'win32', env: { SHUFFLE_LAUNCHER_VERSION: '4' } });
	await main.updater.prepared.prepare();
	const before = main.window.sent.length;
	await assert.rejects(main.updater.prepared.discard(), /EBUSY/);
	const states = main.window.sent.slice(before).filter(([channel]) => channel === 'desktop:worker-state');
	assert.equal(states.length, 1, 'the page is told straight away');
	assert.equal(states[0][1].phase, 'failed');
	assert.match(states[0][1].error, /Quit AI Shuffle and open it again to finish installing the update/);
});

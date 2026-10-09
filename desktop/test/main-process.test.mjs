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
	const app = {
		isPackaged: !!installers,
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
		connection() { return {}; }
		tail() { return []; }
	}
	class FakeUpdater {
		setPolicy() {} setBridge() {} useApplications() {} subscribe() { return () => {}; } onBeforeInstall() {} onInstallFailed() {} onInstallGuard() {} start() {} stop() {}
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
		'./managed-context': { managedContext: () => managed, setManagedContext: context => { managed = context; } },
		'./installed-entry': { installedContext: () => installed },
		'./installed-host': { installedLaunchArgs: () => [] },
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
				async attach() {} async application() { return delegatedApplication; } async databaseStarting() {} async healthy() {}
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
	return { app, appEvents, pages, errors, serviceWorkerClears, get postgres() { return postgres; }, get window() { return mainWindow; }, get worker() { return worker; }, get menu() { return menu; }, get tray() { return tray; }, get trayName() { return trayName; }, get ipc() { return ipc; }, get updater() { return updater; } };
}

function electronFake(app, errors, messageBox) {
	return {
		app,
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
		module, exports: module.exports, require: (id) => (id === 'electron' ? electron : createRequire(import.meta.url)(id)),
		process: { env: {} }, setTimeout: inert, clearTimeout: inert, setInterval: inert, clearInterval: inert, Date, Error
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

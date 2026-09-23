/**
 * AI Shuffle desktop: process entry.
 *
 * One instance, one window, one local worker with its own database. On
 * launch the app starts the bundled PostgreSQL, then the worker (or attaches
 * to one already running on this computer), and opens its web UI. A first
 * run needs no setup: the embedded cluster is created on the spot.
 */
import { app, dialog, nativeTheme, safeStorage, session, shell } from 'electron';
import { QuitGuard } from './quit';
import { showCloseDialog } from './close-dialog';
import { existsSync, mkdirSync } from 'node:fs';
import { managedContext } from './managed-context';
import { installedContext } from './installed-entry';
import { installedLaunchArgs } from './installed-host';
import path from 'node:path';
import { registerIpc, type DesktopState, type PostgresStatus, type WorkerControl } from './ipc';
import { installMenu } from './menu';
import { installPermissionPolicy } from './navigation';
import { PostgresSupervisor, resolvePostgresBinaries } from './postgres';
import { plainCipher, SettingsStore, type Cipher } from './settings';
import { desktopIdentity, stableDataDir } from './identity';
import { installTray } from './tray';
import { Updater } from './updater';
import { MainWindow, PARTITION } from './window';
import { resolveRuntime, WorkerSupervisor, type WorkerState } from './worker';

// Packaged previews use metadata; source tests require an explicit isolated profile.
const isolation = managedContext()?.profile;
const baseIdentity = desktopIdentity(app.isPackaged, require(path.join(app.getAppPath(), 'package.json')));
const identity = isolation ? { ...baseIdentity, preview: true, name: 'AI Shuffle Isolated', appId: 'com.hobbycoders.aishuffle.isolated', port: isolation.port, protectedPort: isolation.protectedPort, allowAttach: false, updates: true } : baseIdentity;
app.setAppUserModelId(identity.appId);
if (isolation) {
	const userData = path.join(managedContext()!.root, 'profile');
	mkdirSync(path.join(userData, 'browser'), { recursive: true });
	app.setName(identity.name);
	app.setPath('userData', userData);
	app.setPath('sessionData', path.join(userData, 'browser'));
} else if (identity.preview) {
	app.setName(identity.name);
	app.setPath('userData', path.join(app.getPath('appData'), identity.name));
}

// Development helpers: an isolated profile and a headless screenshot, so the
// shell can be exercised from a script or CI without a person at the screen.
if (!app.isPackaged && !managedContext() && process.env.SHUFFLE_DESKTOP_USER_DATA) {
	app.setPath('userData', process.env.SHUFFLE_DESKTOP_USER_DATA);
}

if (!app.requestSingleInstanceLock()) {
	app.quit();
} else {
	void main();
}

async function main(): Promise<void> {
	const userData = app.getPath('userData');
	const window = new MainWindow(userData, isolation ? identity.name : 'AI Shuffle', !isolation);
	const updater = new Updater(identity.updates);
	const managed = managedContext();
	const applications = managed?.updates ?? installedContext()?.updates ?? null;
	const installedExecutable = managed?.host?.executable ?? installedContext()?.executable;
	if (applications) process.env.SHUFFLE_UPDATE_POLICY_FILE = path.join(applications.options.root, 'allow-updates.json');
	if (applications) updater.useApplications(applications, installedExecutable, installedLaunchArgs(process.argv.slice(1), app.commandLine.getSwitchValue('remote-debugging-port')).filter(arg => arg.startsWith('--')));

	app.on('second-instance', () => window.focus());
	app.on('window-all-closed', () => {
		if (process.platform !== 'darwin') app.quit();
	});
	if (isolation) { /* Isolated profiles never replace the normal app’s URL handler. */ }
	else if (installedExecutable) app.setAsDefaultProtocolClient(identity.protocol, installedExecutable);
	else if (managed) app.setAsDefaultProtocolClient(identity.protocol, managed.application.node, [path.join(managed.application.desktopDir, 'scripts/bootstrap.mjs'), '--root', managed.root]);
	else if (app.isPackaged) app.setAsDefaultProtocolClient(identity.protocol);

	await app.whenReady();

	const cipher = safeStorageCipher();
	const settings = new SettingsStore(path.join(userData, 'worker-settings.json'), cipher);
	await settings.load();

	// The bundled PostgreSQL: under resources/postgres in a packaged app,
	// SHUFFLE_DESKTOP_POSTGRES_DIR in development. Always app-managed.
	const logDir = path.join(userData, 'logs');
	const pgResolved = resolvePostgresBinaries({ packaged: app.isPackaged || !!managed, resourcesPath: managed?.application.resources ?? process.resourcesPath });
	const postgresError = 'error' in pgResolved ? pgResolved.error : null;
	const postgres =
		'error' in pgResolved
			? null
			: new PostgresSupervisor({
					binaries: pgResolved,
					root: path.join(userData, 'postgres'),
					logDir,
					credentials: {
						load: () => settings.postgres(),
						save: (config) => settings.savePostgres({ ...config, managed: true })
					}
				});

	let supervisor: WorkerSupervisor | null = null;
	let runtimeError: string | null = null;
	let initialized = false;
	let initializing = false;
	let initialization: Promise<void> | null = null;
	async function initializeRuntime(): Promise<void> {
		const repoDir = path.resolve(__dirname, '..', '..', '..');
		const selected = managed?.application;
		const resolved = selected ? {
			mode: 'bundled' as const, root: path.join(selected.resources, 'worker'),
			python: selected.python, serverDir: selected.serverDir,
			nodeBinDir: path.dirname(selected.node), toolsDir: path.join(selected.serverDir, 'tools'),
			browserMcpEntry: path.join(selected.serverDir, 'browser-mcp/dist/index.js'), manifest: null
		} : resolveRuntime({ packaged: app.isPackaged, resourcesPath: process.resourcesPath, repoDir });
		runtimeError = 'error' in resolved ? resolved.error : null;
		supervisor =
			'error' in resolved
				? null
				: new WorkerSupervisor({
						runtime: resolved,
						logDir,
						envFile: path.join(userData, 'worker', '.env'),
						stateDir: path.join(userData, 'worker'),
						dataDir: identity.preview ? path.join(userData, 'data') : stableDataDir(process.platform, process.env, app.getPath('home')),
						...(identity.preview ? { workspaceDir: path.join(userData, 'workspace'), profileDir: path.join(userData, 'profile') } : {}),
						port: identity.port,
						protectedPort: identity.protectedPort,
						isolated: !!isolation,
						allowAttach: applications ? false : identity.allowAttach,
						postgres: () => postgres?.connection() ?? null
					});
		worker.supervisor = supervisor;
		worker.runtimeError = runtimeError;
		observeWorker();
	}
	// Development: point the window at a frontend dev server instead of the worker UI.
	const devUrl = !app.isPackaged ? process.env.SHUFFLE_DESKTOP_DEV_URL ?? null : null;

	const ses = session.fromPartition(PARTITION);
	ses.setUserAgent(`${ses.getUserAgent()} AIShuffleDesktop/${app.getVersion()}`);
	installPermissionPolicy(() => window.currentWorkerUrl);

	const failedState = (error: string): WorkerState => ({
		phase: 'failed',
		port: identity.port,
		url: `http://127.0.0.1:${identity.port}`,
		pid: null,
		external: false,
		error,
		exitCode: null,
		startedAt: null,
		logPath: path.join(logDir, 'worker.log')
	});

	const postgresStatus = (): PostgresStatus => {
		if (!postgres) return { managed: true, phase: 'failed', host: null, port: null, error: postgresError, logPath: null };
		const state = postgres.state;
		return { managed: true, phase: state.phase, host: '127.0.0.1', port: state.port, error: state.error, logPath: state.logPath };
	};

	const desktopState = (): DesktopState => ({
		...(supervisor?.state ?? { ...failedState(runtimeError ?? 'The worker runtime is missing.'), ...(!initialized ? { phase: 'starting' as const, error: null } : {}) }),
		postgres: postgresStatus()
	});

	/** Bundled PostgreSQL first when it is wanted, then the worker. */
	const startStack = async (): Promise<void> => {
		if (quitting || applicationFailed || initializing) return;
		if (!initialized) {
			initializing = true;
			try { initialization = initializeRuntime(); await initialization; }
			catch (error) { runtimeError = String(error); }
			finally { initializing = false; initialized = true; }
			window.send('desktop:worker-state', desktopState());
		}
		if (quitting || !supervisor) return;
		if (!postgres) {
			window.send('desktop:worker-state', desktopState());
			return;
		}
		const state = await postgres.start();
		if (quitting || state.phase !== 'ready') return;
		await supervisor.start();
	};

	const worker: WorkerControl = {
		supervisor,
		runtimeError,
		bundledPostgresError: postgresError,
		state: desktopState,
		tail: () => [...(postgres?.tail().map((line) => `[postgres] ${line}`) ?? []), ...(supervisor?.tail() ?? [])],
		openStatus: () => window.showPage('status'),
		restart: async () => {
			if (!supervisor) { app.relaunch(); app.quit(); return; }
			if (window.currentPage !== 'status') await window.showPage('status');
			await supervisor.stop();
			await startStack();
		}
	};

	updater.setPolicy(() => supervisor?.updatesAllowed() ?? Promise.resolve(false));
	updater.setBridge(state => supervisor?.exchangeUpdateState(state) ?? Promise.resolve({}),
		async () => !!supervisor && await supervisor.activeSessionCount() === 0);
	registerIpc({ window, updater, worker });
	updater.subscribe((state) => window.send('desktop:update-state', state));
	nativeTheme.on('updated', () => window.send('desktop:native-theme', { dark: nativeTheme.shouldUseDarkColors }));

	let quitting = false;
	let applicationFailed = false;
	// A worker that has just answered /health/ready can still refuse its first
	// page load for a moment; retry briefly before showing the failure.
	let loadRetries = 0;
	let claudeNoticeShown = false;
	function observeWorker(): void {
	supervisor?.on('state', (state: WorkerState) => {
		if (quitting) return;
		if (state.phase === 'ready' && state.setupNotice && !claudeNoticeShown) {
			// Claude sessions need the user's own CLI; say how to install it once per launch.
			claudeNoticeShown = true;
			void dialog.showMessageBox({ type: 'info', title: 'AI Shuffle', message: 'Claude Code CLI not installed', detail: state.setupNotice, buttons: ['OK'] });
		}
		if (applications && state.phase === 'ready' && !state.external) {
			// The initial timer may have fired while this revision was still booting.
			void applications.healthy().then(() => updater.check()).catch(error => console.error('Could not record healthy application', error));
		}
		if (applications && state.phase === 'failed') {
			applicationFailed = true;
			void applications.failed().catch(error => console.error('Could not record failed application', error));
		}
		window.send('desktop:worker-state', desktopState());
		// The status page hands over to the worker UI as soon as it answers;
		// a worker that dies while its UI is showing brings the status page back.
		if (state.phase === 'ready' && window.currentPage === 'status') {
			loadRetries = 0;
			void window.loadWorker(state.url);
		}
		if ((state.phase === 'failed' || state.phase === 'stopped') && window.currentPage === null) void window.showPage('status');
	});
	supervisor?.on('log', (line: string) => {
		if (window.currentPage === 'status') window.send('desktop:worker-log', line);
	});
	}
	postgres?.on('state', () => {
		if (quitting) return;
		window.send('desktop:worker-state', desktopState());
	});
	postgres?.on('log', (line: string) => {
		if (window.currentPage === 'status') window.send('desktop:worker-log', `[postgres] ${line}`);
	});
	window.onLoadFailed((message) => {
		const state = worker.state();
		if (state.phase === 'ready' && loadRetries < 6) {
			loadRetries++;
			setTimeout(() => void window.loadWorker(state.url), 1500);
			return;
		}
		loadRetries = 0;
		void window.showPage('status', { error: message });
	});

	const actions = {
		command: (name: string) => window.send('desktop:command', name),
		restartWorker: () => void worker.restart(),
		openWorkerStatus: () => void worker.openStatus(),
		updatesEnabled: identity.updates,
		showLogs: () => void shell.showItemInFolder(worker.state().logPath),
		showPostgresLogs: () => {
			const logPath = postgres?.state.logPath ?? path.join(logDir, 'postgres.log');
			if (existsSync(logPath)) void shell.showItemInFolder(logPath);
			else void shell.openPath(logDir);
		},
		openInBrowser: () => {
			const state = worker.state();
			if (state.phase === 'ready') window.openExternal(state.url);
		},
		checkForUpdates: () => void updater.check(true),
		reload: () => window.browserWindow?.webContents.reload(),
		openDocs: () => {
			if (window.currentWorkerUrl) window.send('desktop:command', 'open-docs');
			else window.openExternal('https://github.com/HobbyCoders/ai-shuffle#readme');
		},
		show: () => {
			window.focus();
			void open();
		}
	};
	installMenu(actions);
	if (!isolation) installTray(actions);

	updater.onBeforeInstall(async () => {
		await initialization;
		if (supervisor && !supervisor.state.external) {
			await supervisor.stop();
			await supervisor.clearConnectionFile();
		}
		await postgres?.stop();
	});

	const quitGuard = new QuitGuard({
		activeSessionCount: () => supervisor?.activeSessionCount() ?? Promise.resolve(0),
		confirm: (activeCount) => showCloseDialog(window.browserWindow, activeCount),
		stop: async () => {
			quitting = true;
			try {
				await initialization?.catch(() => {});
				// Do not stop the database if session cleanup failed. Keep the app
				// open so the user can retry rather than stranding another session.
				if (supervisor && !supervisor.state.external) await supervisor.stop();
				if (postgres) await postgres.stop();
				if (supervisor && !supervisor.state.external) await supervisor.clearConnectionFile();
				updater.stop();
			} catch (error) {
				quitting = false;
				throw error;
			}
		},
		quit: () => app.quit(),
		failed: async () => {
			await showCloseDialog(window.browserWindow, null, true).catch(() => {
				// A failed renderer must never turn an error into permission to quit.
			});
		}
	});
	// Windows/Linux window close must be intercepted BEFORE its renderer dies.
	// On macOS closing the window leaves the app running; Cmd+Q uses before-quit.
	window.onClose = (event) => {
		if (process.platform === 'darwin' || quitGuard.canQuit) return;
		event.preventDefault();
		void quitGuard.request();
	};
	app.on('before-quit', (event) => {
		if (quitGuard.canQuit) return;
		event.preventDefault();
		void quitGuard.request();
	});
	app.on('activate', () => void open());

	await open();
	updater.start();
	installScreenshotHook(window);

	async function open(): Promise<void> {
		if (window.browserWindow) {
			window.focus();
			return;
		}
		if (devUrl) {
			await window.loadWorker(devUrl);
			return;
		}
		await window.showPage('status');
		void startStack();
	}
}

function safeStorageCipher(): Cipher {
	try {
		if (!safeStorage.isEncryptionAvailable()) return plainCipher;
	} catch {
		return plainCipher;
	}
	return {
		available: () => true,
		encrypt: (text) => safeStorage.encryptString(text),
		decrypt: (data) => safeStorage.decryptString(data)
	};
}

/**
 * `SHUFFLE_DESKTOP_SHOT=/path/out.png` captures the window a few seconds after
 * load and quits. `SHUFFLE_DESKTOP_SHOT_JS` runs a snippet in the page first
 * (for example to open a feature page) and waits a moment for it to settle.
 */
function installScreenshotHook(window: MainWindow): void {
	const target = process.env.SHUFFLE_DESKTOP_SHOT;
	if (app.isPackaged || !target) return;
	const delay = Number(process.env.SHUFFLE_DESKTOP_SHOT_DELAY ?? 4000);
	const script = process.env.SHUFFLE_DESKTOP_SHOT_JS;
	setTimeout(async () => {
		try {
			const win = window.browserWindow;
			if (!win) return;
			if (script) {
				await win.webContents.executeJavaScript(script, true);
				await new Promise((resolve) => setTimeout(resolve, 2000));
			}
			const fs = await import('node:fs/promises');
			try {
				const image = await win.webContents.capturePage();
				await fs.writeFile(target, image.toPNG());
				console.log(`screenshot written to ${target}`);
			} catch (error) {
				// Headless CI (xvfb, no GPU) sometimes cannot composite a capture.
				// Record what loaded instead so the smoke run still proves the
				// shell booted and rendered the page.
				const title = await win.webContents.executeJavaScript('document.title', true).catch(() => '');
				const report = [`capture failed: ${String(error)}`, `url: ${win.webContents.getURL()}`, `title: ${title}`, ''];
				await fs.writeFile(`${target}.txt`, report.join('\n'));
				console.log(`screenshot unavailable, wrote ${target}.txt`);
			}
		} catch (error) {
			console.error('screenshot hook failed', error);
		} finally {
			app.quit();
		}
	}, delay);
}

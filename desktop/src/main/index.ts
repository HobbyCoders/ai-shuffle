/**
 * AI Shuffle desktop: process entry.
 *
 * One instance, one window, one local worker with its own database. On
 * launch the app starts the bundled PostgreSQL, then the worker (or attaches
 * to one already running on this computer), and opens its web UI. A first
 * run needs no setup: the embedded cluster is created on the spot.
 */
import { app, dialog, safeStorage, session, shell } from 'electron';
import { QuitGuard } from './quit';
import { showCloseDialog } from './close-dialog';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { managedContext } from './managed-context';
import { installedContext } from './installed-entry';
import { installedLaunchArgs } from './installed-host';
import path from 'node:path';
import { registerIpc, type DesktopState, type PostgresStatus, type WorkerControl } from './ipc';
import { installMenu } from './menu';
import { forgetServiceWorkers, installPermissionPolicy } from './navigation';
import { installListenerHeader, type ListenerTarget } from './listener';
import { AGENT_ACCESS, agentAccessDetail, DisclosureStore, ensureAgentAccessAcknowledged } from './disclosure';
import { PostgresSupervisor, resolvePostgresBinaries } from './postgres';
import { plainCipher, SettingsStore, type Cipher } from './settings';
import { desktopIdentity, stableDataDir } from './identity';
import { installTray } from './tray';
import { Updater } from './updater';
import { ManualUpdateCheck, type FleetReceipt, type FleetSnapshot } from './manual-update-check';
import { InstallerUpdates, installerPlatform } from './installer-updates';
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
	// A startup failure must not leave a windowless process behind.
	main().catch((error) => {
		dialog.showErrorBox('AI Shuffle could not start', error instanceof Error ? error.message : String(error));
		app.quit();
	});
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
	// No aishuffle:// link is produced anywhere and the app never read one, so
	// the scheme is no longer registered: it only let any web page launch the
	// app with arguments of its choosing. Drop the handler older builds set.
	if (!isolation) forgetProtocolHandler(identity.protocol, installedExecutable, managed);

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
	/** Set once this launch starts the database; a first boot quit before then cannot have migrated it. */
	let stackStarted = false;
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
		observeWorker();
	}
	// Development: point the window at a frontend dev server instead of the worker UI.
	const devUrl = !app.isPackaged ? process.env.SHUFFLE_DESKTOP_DEV_URL ?? null : null;

	const ses = session.fromPartition(PARTITION);
	ses.setUserAgent(`${ses.getUserAgent()} AIShuffleDesktop/${app.getVersion()}`);
	installPermissionPolicy(() => window.currentWorkerUrl);
	// Requests to the worker from its own UI (and top-level loads of it) carry the listener token.
	const listenerTarget = (): ListenerTarget | null => {
		const token = supervisor?.listenerToken;
		if (!supervisor || !token) return null;
		// pageOrigin: a development run's frontend dev server; the worker's own origin otherwise.
		try { return { origin: new URL(supervisor.state.url).origin, token, pageOrigin: window.currentWorkerUrl }; } catch { return null; }
	};
	installListenerHeader(ses, listenerTarget);
	// A service worker an earlier version let a pop-up register would own the
	// window's requests; the app never uses one, so none survives a launch.
	await forgetServiceWorkers(ses);

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
		if (quitting || applicationFailed || initializing || !accepted) return;
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
		stackStarted = true;
		// From here a first boot may migrate the database, so a crash must block it.
		// Unrecorded, a crash would look harmless to the launcher: do not start.
		try { await applications?.databaseStarting(); }
		catch (error) {
			runtimeError = `Could not record the application start: ${error instanceof Error ? error.message : String(error)}`;
			supervisor = null;
			window.send('desktop:worker-state', desktopState());
			return;
		}
		const state = await postgres.start();
		if (quitting || state.phase !== 'ready') return;
		await supervisor.start();
	};

	const worker: WorkerControl = {
		state: desktopState,
		tail: () => [...(postgres?.tail().map((line) => `[postgres] ${line}`) ?? []), ...(supervisor?.tail() ?? [])],
		restart: async () => {
			if (!supervisor) { app.relaunch(); app.quit(); return; }
			if (window.currentPage !== 'status') await window.showPage('status');
			try {
				await supervisor.stop();
			} catch (error) {
				// Sessions could not be stopped, so the worker is still running: go back to its UI.
				const state = supervisor.state;
				if (state.phase === 'ready' && window.currentPage === 'status') {
					loadRetries = 0;
					void window.loadWorker(state.url);
				}
				throw error;
			}
			await startStack();
		},
		// "Worker Status…" can show the status page while the worker runs; its
		// ready state never changes then, so the page asks to go back itself.
		openUi: async () => {
			const state = supervisor?.state;
			if (state?.phase !== 'ready' || window.currentPage !== 'status') return;
			loadRetries = 0;
			await window.loadWorker(state.url);
		}
	};

	updater.setPolicy(() => supervisor?.updatesAllowed() ?? Promise.resolve(false));
	// The hub's gated feed wants this worker's license lease; older source managers lack useLease.
	applications?.useLease?.(() => supervisor?.sourceLease() ?? Promise.resolve(null));
	updater.setBridge(state => supervisor?.exchangeUpdateState(state) ?? Promise.resolve({}),
		async () => !!supervisor && await supervisor.activeSessionCount() === 0);
	// macOS and Linux: new releases come as signed installers (D22: checked on
	// launch and daily without asking). Windows keeps its source-update path.
	// The menu goes through the worker's update dispatch (the web button's), so
	// Update all workers applies; the local updater is the fallback only.
	const manualUpdateCheck = new ManualUpdateCheck(updater, options => dialog.showMessageBox(options), {
		workers: async () => (await supervisor?.updateWorkers() ?? null) as FleetSnapshot | null,
		act: async action => {
			if (!supervisor) throw new Error('The worker is not running');
			return await supervisor.updateAction(action) as unknown as FleetReceipt;
		},
	});
	const platform = installerPlatform();
	if (!applications && platform && app.isPackaged && !isolation) {
		try {
			const publicKey = readFileSync(path.join(app.getAppPath(), 'build/application-update-key.pem'), 'utf8');
			updater.useInstallers(new InstallerUpdates({ platform, arch: process.arch, currentVersion: app.getVersion(), publicKey, directory: app.getPath('downloads') }),
				file => shell.showItemInFolder(file));
			let offered: string | null = null;
			updater.subscribe((state) => {
				// Offer each new release once per launch; the sidebar button keeps offering it.
				if (manualUpdateCheck.active || state.phase !== 'available' || !state.version || state.version === offered) return;
				offered = state.version;
				void dialog.showMessageBox({ type: 'info', title: 'AI Shuffle', message: `AI Shuffle ${state.version} is available`,
					detail: 'Download the installer now? It is checked against the signed release before it is saved to your Downloads folder.',
					buttons: ['Download', 'Later'], defaultId: 0, cancelId: 1 })
					.then(({ response }) => { if (response === 0) void updater.download(); });
			});
		} catch (error) {
			console.error('Installer update checks are unavailable', error);
		}
	}
	registerIpc({ window, updater, worker });
	updater.subscribe((state) => window.send('desktop:update-state', state));

	let quitting = false;
	let applicationFailed = false;
	/** Set once the agent-access disclosure is accepted; no database or worker before that. */
	let accepted = false;
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
		restartWorker: () => void worker.restart().catch((error) => dialog.showErrorBox('Restart Worker', error instanceof Error ? error.message : String(error))),
		openWorkerStatus: () => void window.showPage('status'),
		updatesEnabled: identity.updates,
		showLogs: () => void shell.showItemInFolder(worker.state().logPath),
		showPostgresLogs: () => {
			const logPath = postgres?.state.logPath ?? path.join(logDir, 'postgres.log');
			if (existsSync(logPath)) void shell.showItemInFolder(logPath);
			else void shell.openPath(logDir);
		},
		openInBrowser: () => {
			const state = worker.state();
			if (state.phase !== 'ready') return;
			// The worker refuses requests without the listener token, which only
			// this app's window sends: hand the browser a one-time session URL.
			void (supervisor ? supervisor.browserHandoffUrl() : Promise.resolve(null)).then(
				(url) => window.openExternal(url ?? state.url),
				(error) => dialog.showErrorBox('Open in Browser', error instanceof Error ? error.message : String(error))
			);
		},
		checkForUpdates: () => {
			window.focus();
			void manualUpdateCheck.run().catch(error => console.error('Could not show update check result', error));
		},
		reload: () => window.browserWindow?.webContents.reload(),
		showAgentAccess: () => void showAgentAccess(false),
		openDocs: () => {
			if (window.currentWorkerUrl) window.send('desktop:command', 'open-docs');
			else window.openExternal('https://github.com/HobbyCoders/ai-shuffle#readme');
		},
		show: () => {
			window.focus();
			void open();
		},
		// The tray's Quit: the graceful stop first, then a kill if the worker is wedged.
		forceQuit: () => void quitGuard.request({ force: true })
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
	// Restarting into an update stops the worker's sessions: ask first while any run.
	updater.onConfirmInstall(async () => {
		let active: number | null;
		try { active = supervisor ? await supervisor.activeSessionCount() : 0; }
		catch { active = null; }
		if (active === 0) return true;
		const activity = active === null ? 'AI Shuffle could not check for active sessions.' : `You have ${active} active session${active === 1 ? '' : 's'}.`;
		const options = {
			type: 'warning' as const, title: 'AI Shuffle', message: 'Restart to install the update?',
			detail: `${activity} Restarting stops ${active === 1 ? 'it' : 'any active sessions'}.`,
			buttons: ['Stop Sessions and Restart', 'Cancel'], defaultId: 1, cancelId: 1, noLink: true
		};
		const parent = window.browserWindow;
		const { response } = parent ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options);
		return response === 0;
	});
	// An install that failed after the step above leaves the stack down: start it again.
	updater.onInstallFailed(async (error) => {
		const reason = error instanceof Error ? error.message : String(error);
		try {
			await startStack();
		} catch (failure) {
			dialog.showErrorBox('AI Shuffle could not restart', `The update could not be installed: ${reason}\n\nStarting AI Shuffle again also failed: ${failure instanceof Error ? failure.message : String(failure)}`);
			return;
		}
		dialog.showErrorBox('Update could not be installed', reason);
	});

	// Set once a quit kills the worker; a graceful stop still settling in the
	// background must not reopen the app's state after that.
	let forcedQuit = false;
	// A first boot that never reached the database is retried on the next launch, not marked failed.
	const deferBoot = async (): Promise<void> => {
		if (stackStarted || !applications) return;
		await applications.deferBoot().catch(error => console.error('Could not record the unstarted application', error));
	};
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
				await deferBoot();
			} catch (error) {
				if (!forcedQuit) quitting = false;
				throw error;
			}
		},
		forceStop: async () => {
			quitting = true;
			forcedQuit = true;
			updater.stop();
			// The worker never confirmed its sessions stopped: end it without asking again.
			if (supervisor && !supervisor.state.external) await supervisor.stop({ force: true });
			if (postgres) await postgres.stop();
			if (supervisor && !supervisor.state.external) await supervisor.clearConnectionFile();
			await deferBoot();
		},
		quit: () => app.quit(),
		failed: (_error, offerForce) =>
			// A failed renderer must never turn an error into permission to quit.
			showCloseDialog(window.browserWindow, null, true, offerForce).catch(() => false)
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

	// "Agents run as you": once, before the first worker ever starts.
	// Until it is accepted, a dock click or the tray's Open must not start the worker.
	// Isolated test profiles are driven by scripts, not people.
	if (!isolation && !(await ensureAgentAccessAcknowledged(DisclosureStore.forProfile(userData), () => showAgentAccess(true)))) {
		app.quit();
		return;
	}

	accepted = true;
	await open();
	updater.start();
	installScreenshotHook(window);

	async function showAgentAccess(firstRun: boolean): Promise<'continue' | 'quit'> {
		const options = {
			type: 'warning' as const,
			title: AGENT_ACCESS.title,
			message: AGENT_ACCESS.message,
			detail: agentAccessDetail(),
			buttons: firstRun ? ['I understand, continue', 'Quit'] : ['OK'],
			defaultId: 0,
			cancelId: firstRun ? 1 : 0,
			noLink: true
		};
		if (firstRun) {
			// Older installed launchers start this process with SW_HIDE, which Windows
			// applies to the first window shown. A parentless dialog would be that
			// window and stay invisible, so show the main window (which retries a
			// suppressed show) first and let it own the dialog. Nothing starts yet.
			await window.showPage('status');
			window.focus();
		}
		const parent = window.browserWindow;
		const { response } = parent ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options);
		return response === 0 ? 'continue' : 'quit';
	}

	async function open(): Promise<void> {
		if (!accepted) return;
		if (window.browserWindow) {
			window.focus();
			// The first-run disclosure showed this window before anything started.
			if (!initialized) void startStack();
			return;
		}
		if (devUrl) {
			await window.loadWorker(devUrl);
			return;
		}
		// macOS keeps the app and its worker running after the window closes;
		// a new window goes straight back to the running worker.
		const state = supervisor?.state;
		if (state?.phase === 'ready') {
			loadRetries = 0;
			await window.loadWorker(state.url);
			return;
		}
		await window.showPage('status');
		void startStack();
	}
}

/** Remove this build's own aishuffle:// registration from an earlier version, if it is still there. */
function forgetProtocolHandler(protocol: string, installedExecutable: string | undefined, managed: ReturnType<typeof managedContext>): void {
	const handler: [string?, string[]?] | null = installedExecutable ? [installedExecutable]
		: managed ? [managed.application.node, [path.join(managed.application.desktopDir, 'scripts/bootstrap.mjs'), '--root', managed.root]]
			: app.isPackaged ? [] : null;
	if (!handler) return;
	try {
		if (app.isDefaultProtocolClient(protocol, ...handler)) app.removeAsDefaultProtocolClient(protocol, ...handler);
	} catch (error) {
		console.error('Could not remove the old aishuffle:// handler', error);
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

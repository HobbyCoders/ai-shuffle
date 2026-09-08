/**
 * AI Shuffle desktop: process entry.
 *
 * One instance, one window, one local worker with its own database. On
 * launch the app starts the bundled PostgreSQL, then the worker (or attaches
 * to one already running on this computer), and opens its web UI. A first
 * run needs no setup: the cluster is created on the spot. People who would
 * rather use their own PostgreSQL choose it under Worker → Advanced.
 */
import { app, nativeTheme, safeStorage, session, shell } from 'electron';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { registerIpc, type DesktopState, type PostgresStatus, type WorkerControl } from './ipc';
import { installMenu } from './menu';
import { installPermissionPolicy } from './navigation';
import { PostgresSupervisor, resolvePostgresBinaries } from './postgres';
import { plainCipher, SettingsStore, type Cipher, type PostgresConfig } from './settings';
import { installTray } from './tray';
import { Updater } from './updater';
import { MainWindow, PARTITION } from './window';
import { resolveRuntime, WorkerSupervisor, type WorkerState } from './worker';

const PROTOCOL = 'aishuffle';

app.setAppUserModelId('com.hobbycoders.aishuffle.desktop');

// Development helpers: an isolated profile and a headless screenshot, so the
// shell can be exercised from a script or CI without a person at the screen.
if (!app.isPackaged && process.env.SHUFFLE_DESKTOP_USER_DATA) {
	app.setPath('userData', process.env.SHUFFLE_DESKTOP_USER_DATA);
}

if (!app.requestSingleInstanceLock()) {
	app.quit();
} else {
	void main();
}

async function main(): Promise<void> {
	const userData = app.getPath('userData');
	const window = new MainWindow(userData);
	const updater = new Updater();

	app.on('second-instance', () => window.focus());
	app.on('window-all-closed', () => {
		if (process.platform !== 'darwin') app.quit();
	});
	if (app.isPackaged) app.setAsDefaultProtocolClient(PROTOCOL);

	await app.whenReady();

	const cipher = safeStorageCipher();
	const settings = new SettingsStore(path.join(userData, 'worker-settings.json'), cipher);
	await settings.load();

	// The bundled PostgreSQL: under resources/postgres in a packaged app,
	// SHUFFLE_DESKTOP_POSTGRES_DIR in development. Started only while the
	// settings ask for the managed server; an external one leaves it alone.
	const logDir = path.join(userData, 'logs');
	const pgResolved = resolvePostgresBinaries({ packaged: app.isPackaged, resourcesPath: process.resourcesPath });
	const postgresError = 'error' in pgResolved ? pgResolved.error : null;
	const postgres =
		'error' in pgResolved
			? null
			: new PostgresSupervisor({
					binaries: pgResolved,
					root: path.join(userData, 'postgres'),
					logDir,
					credentials: {
						load: () => (settings.managed ? settings.postgres() : null),
						save: (config) => settings.savePostgres({ ...config, managed: true })
					}
				});

	// The worker runtime: bundled in a packaged app, the repository in development.
	const repoDir = path.resolve(__dirname, '..', '..', '..');
	const resolved = resolveRuntime({ packaged: app.isPackaged, resourcesPath: process.resourcesPath, repoDir });
	const runtimeError = 'error' in resolved ? resolved.error : null;
	const supervisor =
		'error' in resolved
			? null
			: new WorkerSupervisor({
					runtime: resolved,
					logDir,
					envFile: path.join(userData, 'worker', '.env'),
					// The managed connection comes from the running server, not the
					// file: it is authoritative even when the keyring cannot be read.
					postgres: () => (settings.managed ? postgres?.connection() ?? null : settings.postgres())
				});
	// Development: point the window at a frontend dev server instead of the worker UI.
	const devUrl = !app.isPackaged ? process.env.SHUFFLE_DESKTOP_DEV_URL ?? null : null;

	const ses = session.fromPartition(PARTITION);
	ses.setUserAgent(`${ses.getUserAgent()} AIShuffleDesktop/${app.getVersion()}`);
	installPermissionPolicy(() => window.currentWorkerUrl);

	const failedState = (error: string): WorkerState => ({
		phase: 'failed',
		port: 8000,
		url: 'http://127.0.0.1:8000',
		pid: null,
		external: false,
		error,
		exitCode: null,
		startedAt: null,
		logPath: path.join(logDir, 'worker.log')
	});

	const postgresStatus = (): PostgresStatus => {
		if (!settings.managed) {
			const external = settings.postgresPublic();
			return { managed: false, phase: 'external', host: external.host, port: external.port, error: null, logPath: null };
		}
		if (!postgres) return { managed: true, phase: 'failed', host: null, port: null, error: postgresError, logPath: null };
		const state = postgres.state;
		return { managed: true, phase: state.phase, host: '127.0.0.1', port: state.port, error: state.error, logPath: state.logPath };
	};

	const desktopState = (): DesktopState => ({
		...(supervisor?.state ?? failedState(runtimeError ?? 'The worker runtime is missing.')),
		postgres: postgresStatus()
	});

	/** Bundled PostgreSQL first when it is wanted, then the worker. */
	const startStack = async (): Promise<void> => {
		if (!supervisor) return;
		if (settings.managed) {
			if (!postgres) {
				window.send('desktop:worker-state', desktopState());
				return;
			}
			const state = await postgres.start();
			if (state.phase !== 'ready') return;
		}
		await supervisor.start();
	};

	const worker: WorkerControl = {
		supervisor,
		runtimeError,
		bundledPostgresError: postgresError,
		state: desktopState,
		tail: () => [...(postgres?.tail().map((line) => `[postgres] ${line}`) ?? []), ...(supervisor?.tail() ?? [])],
		openSetup: () => window.showPage('setup'),
		cancelSetup: async () => {
			const state = worker.state();
			if (state.phase === 'ready') await window.loadWorker(state.url);
			else await window.showPage('status');
		},
		saveAndStart: async (config: PostgresConfig) => {
			await settings.savePostgres({ ...config, managed: false });
			await window.showPage('status');
			if (!supervisor) return;
			await supervisor.stop();
			// The bundled server is no longer wanted; leave its data in place.
			await postgres?.stop();
			await startStack();
		},
		useBundled: async () => {
			await settings.useBundled();
			await window.showPage('status');
			if (!supervisor) return;
			await supervisor.stop();
			await startStack();
		},
		restart: async () => {
			if (!supervisor) return;
			if (window.currentPage !== 'status') await window.showPage('status');
			await supervisor.stop();
			await startStack();
		}
	};

	registerIpc({ window, settings, updater, worker, keyringAvailable: cipher.available() });
	updater.subscribe((state) => window.send('desktop:update-state', state));
	nativeTheme.on('updated', () => window.send('desktop:native-theme', { dark: nativeTheme.shouldUseDarkColors }));

	let quitting = false;
	// A worker that has just answered /health/ready can still refuse its first
	// page load for a moment; retry briefly before showing the failure.
	let loadRetries = 0;
	supervisor?.on('state', (state: WorkerState) => {
		if (quitting) return;
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
		openDatabaseSettings: () => void worker.openSetup(),
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
		checkForUpdates: () => void updater.check(),
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
	installTray(actions);

	app.on('before-quit', (event) => {
		if (quitting) return;
		const stopWorker = supervisor !== null && !supervisor.state.external;
		const stopPostgres = postgres !== null && (postgres.state.phase === 'starting' || postgres.state.phase === 'ready' || postgres.state.phase === 'failed');
		if (!stopWorker && !stopPostgres) return;
		event.preventDefault();
		quitting = true;
		void (async () => {
			if (stopWorker) await supervisor.stop();
			if (stopPostgres) await postgres.stop();
		})().finally(() => app.quit());
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

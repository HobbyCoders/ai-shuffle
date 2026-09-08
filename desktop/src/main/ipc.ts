/**
 * IPC handlers behind the preload bridge.
 *
 * Every handler checks that the message comes from the main window. The
 * database setup and the worker's raw log are additionally limited to the
 * bundled pages (file://), so the worker's web UI can steer its own window
 * but never read the database credentials this app holds.
 */
import { app, ipcMain, Notification, shell, type IpcMainEvent, type IpcMainInvokeEvent } from 'electron';
import type { PostgresPhase } from './postgres';
import { validatePostgres, type PostgresConfig, type SettingsStore } from './settings';
import type { Updater } from './updater';
import { MainWindow, TITLE_BAR_HEIGHT, type TitleBarTheme } from './window';
import { checkPostgres, type WorkerState, type WorkerSupervisor } from './worker';

/** The database behind the worker, as the status page shows it. */
export interface PostgresStatus {
	/** True when the app's own bundled PostgreSQL is in use. */
	managed: boolean;
	/** The supervisor's phase, or `external` for a server the person configured. */
	phase: PostgresPhase | 'external';
	host: string | null;
	port: number | null;
	error: string | null;
	logPath: string | null;
}

export type DesktopState = WorkerState & { postgres: PostgresStatus };

export interface WorkerControl {
	supervisor: WorkerSupervisor | null;
	/** Why no supervisor exists (runtime missing), shown on the status page. */
	runtimeError: string | null;
	/** Whether the bundled PostgreSQL can be used, and if not, why. */
	bundledPostgresError: string | null;
	state(): DesktopState;
	/** The supervisor lines followed by the worker's, for the status page log. */
	tail(): string[];
	openSetup(): Promise<void>;
	cancelSetup(): Promise<void>;
	saveAndStart(config: PostgresConfig): Promise<void>;
	useBundled(): Promise<void>;
	restart(): Promise<void>;
}

interface Deps {
	window: MainWindow;
	settings: SettingsStore;
	updater: Updater;
	worker: WorkerControl;
	keyringAvailable: boolean;
}

type Handler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;

export function registerIpc({ window, settings, updater, worker, keyringAvailable }: Deps): void {
	const fromMainWindow = (event: IpcMainInvokeEvent | IpcMainEvent) => window.isOwnSender(event.sender.id);
	const fromBundledPage = (event: IpcMainInvokeEvent | IpcMainEvent) =>
		fromMainWindow(event) && (event.senderFrame?.url ?? '').startsWith('file:');

	const handle = (channel: string, guard: (event: IpcMainInvokeEvent) => boolean, fn: Handler) => {
		ipcMain.handle(channel, (event, ...args) => {
			if (!guard(event)) throw new Error(`${channel}: not allowed from this page`);
			return fn(event, ...args);
		});
	};

	// Synchronous so the bridge can expose plain properties at preload time.
	ipcMain.on('desktop:info', (event) => {
		if (!fromMainWindow(event)) {
			event.returnValue = null;
			return;
		}
		const state = worker.state();
		event.returnValue = {
			platform: process.platform,
			version: app.getVersion(),
			titleBarHeight: TITLE_BAR_HEIGHT,
			worker: { url: state.url, phase: state.phase, external: state.external },
			windowState: window.windowState(),
			updateState: updater.current
		};
	});

	// Window and shell.
	handle('desktop:window-action', fromMainWindow, (_e, action) => {
		const win = window.browserWindow;
		if (!win) return;
		switch (action) {
			case 'minimize':
				win.minimize();
				break;
			case 'toggle-maximize':
				win.isMaximized() ? win.unmaximize() : win.maximize();
				break;
			case 'close':
				win.close();
				break;
			case 'focus':
				window.focus();
				break;
		}
	});
	handle('desktop:window-state', fromMainWindow, () => window.windowState());
	handle('desktop:set-title-bar-theme', fromMainWindow, (_e, theme) => {
		if (!isTitleBarTheme(theme)) return;
		window.setTitleBarTheme(theme);
	});
	handle('desktop:open-external', fromMainWindow, (_e, url) => {
		if (typeof url === 'string') window.openExternal(url);
	});
	handle('desktop:set-badge', fromMainWindow, (_e, count) => {
		const n = typeof count === 'number' && Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
		if (process.platform === 'win32') {
			window.browserWindow?.flashFrame(n > 0);
			return;
		}
		app.setBadgeCount(n);
	});
	handle('desktop:notify', fromMainWindow, (_e, payload) => {
		if (!Notification.isSupported() || !payload || typeof payload !== 'object') return;
		const p = payload as { title?: unknown; body?: unknown; tag?: unknown; silent?: unknown };
		const title = typeof p.title === 'string' ? p.title.slice(0, 120) : 'AI Shuffle';
		const body = typeof p.body === 'string' ? p.body.slice(0, 400) : '';
		const tag = typeof p.tag === 'string' ? p.tag.slice(0, 200) : null;
		const notification = new Notification({ title, body, silent: p.silent === true });
		notification.on('click', () => {
			window.focus();
			window.send('desktop:notification-click', { tag });
		});
		notification.show();
	});

	// Worker state is visible to every page; control is too, since the web UI
	// is the worker's own and restarting it is not a secret.
	handle('desktop:worker-state', fromMainWindow, () => worker.state());
	handle('desktop:worker-restart', fromMainWindow, () => worker.restart());
	handle('desktop:worker-open-logs', fromMainWindow, () => void shell.showItemInFolder(worker.state().logPath));
	handle('desktop:worker-open-setup', fromMainWindow, () => worker.openSetup());

	// Updates.
	handle('desktop:update-state', fromMainWindow, () => updater.current);
	handle('desktop:update-check', fromMainWindow, () => updater.check());
	handle('desktop:update-install', fromMainWindow, () => updater.install());

	// Bundled pages only: database setup and the raw log.
	handle('desktop:settings-get', fromBundledPage, () => ({
		postgres: settings.postgresPublic(),
		configured: settings.configured,
		managed: settings.managed,
		bundledAvailable: worker.bundledPostgresError === null,
		bundledError: worker.bundledPostgresError,
		keyringAvailable,
		runtimeError: worker.runtimeError
	}));
	handle('desktop:db-check', fromBundledPage, (_e, input, action) => {
		const validated = validatePostgres((input ?? {}) as Record<string, unknown>);
		if (!validated.ok) return { ok: false, code: 'unknown', error: validated.error };
		if (!worker.supervisor) return { ok: false, code: 'runtime', error: worker.runtimeError ?? 'The worker runtime is missing.' };
		return checkPostgres(worker.supervisor.runtime, validated.config, action === 'create' ? 'create' : 'test');
	});
	handle('desktop:setup-save', fromBundledPage, async (_e, input) => {
		const validated = validatePostgres((input ?? {}) as Record<string, unknown>);
		if (!validated.ok) throw new Error(validated.error);
		await worker.saveAndStart(validated.config);
	});
	handle('desktop:setup-cancel', fromBundledPage, () => worker.cancelSetup());
	handle('desktop:setup-use-bundled', fromBundledPage, () => worker.useBundled());
	handle('desktop:worker-tail', fromBundledPage, () => worker.tail());
}

function isTitleBarTheme(value: unknown): value is TitleBarTheme {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	const hex = /^#[0-9a-fA-F]{6}$/;
	return typeof v.background === 'string' && hex.test(v.background) && typeof v.symbol === 'string' && hex.test(v.symbol);
}

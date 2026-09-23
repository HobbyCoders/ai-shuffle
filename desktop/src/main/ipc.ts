/**
 * IPC handlers behind the preload bridge.
 *
 * Every handler checks that the message comes from the main window. The
 * worker's raw log is additionally limited to the bundled status page
 * (file://). No renderer can read or change database credentials.
 */
import { app, ipcMain, Notification, shell, type IpcMainEvent, type IpcMainInvokeEvent } from 'electron';
import type { PostgresPhase } from './postgres';
import type { Updater } from './updater';
import { managedContext } from './managed-context';
import { desktopSampler } from './diagnostics';
import { MainWindow, TITLE_BAR_HEIGHT, type TitleBarTheme } from './window';
import { type WorkerState, type WorkerSupervisor } from './worker';
import { LISTENER_HEADER, type ListenerTarget } from './listener';
import { AGENT_ACCESS } from './disclosure';

/** The database behind the worker, as the status page shows it. */
export interface PostgresStatus {
	/** True when the app's own bundled PostgreSQL is in use. */
	managed: boolean;
	/** The embedded supervisor's phase. */
	phase: PostgresPhase;
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
	/** The worker origin and listener token this window sends, or null. */
	listener(): ListenerTarget | null;
	/** The supervisor lines followed by the worker's, for the status page log. */
	tail(): string[];
	openStatus(): Promise<void>;
	restart(): Promise<void>;
}

interface Deps {
	window: MainWindow;
	updater: Updater;
	worker: WorkerControl;
}

type Handler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;

export function registerIpc({ window, updater, worker }: Deps): void {
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
			version: managedContext() ? `main@${managedContext()!.application.revision.slice(0, 12)}` : app.getVersion(),
			titleBarHeight: TITLE_BAR_HEIGHT,
			worker: { url: state.url, phase: state.phase, external: state.external },
			windowState: window.windowState(),
			updateState: updater.current,
			agentAccess: AGENT_ACCESS
		};
	});

	// The worker's own top-level page only. The session already sends the
	// token on every request to the worker; this is for anything that has to
	// leave the session (for example a URL handed to another program).
	const fromWorkerMainFrame = (event: IpcMainInvokeEvent) => {
		if (!fromMainWindow(event) || event.senderFrame !== event.sender.mainFrame) return false;
		try {
			return !!window.currentWorkerUrl && new URL(event.senderFrame.url).origin === new URL(window.currentWorkerUrl).origin;
		} catch { return false; }
	};
	handle('desktop:listener-token', fromWorkerMainFrame, () => {
		const target = worker.listener();
		if (!target || target.origin !== new URL(window.currentWorkerUrl!).origin) return null;
		return { header: LISTENER_HEADER, token: target.token };
	});

	// Opt-in only, with the existing sender guard plus a main-frame origin check.
	const sampleDesktop = desktopSampler(() => app.getAppMetrics());
	handle('desktop:diagnostics', event => {
		if (!fromMainWindow(event) || event.senderFrame !== event.sender.mainFrame) return false;
		if (fromBundledPage(event)) return true;
		try {
			return !!window.currentWorkerUrl && new URL(event.senderFrame.url).origin === new URL(window.currentWorkerUrl).origin;
		} catch { return false; }
	}, () => sampleDesktop());

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
	handle('desktop:worker-open-status', fromMainWindow, () => worker.openStatus());

	// Updates.
	handle('desktop:update-state', fromMainWindow, () => updater.current);
	handle('desktop:update-check', fromMainWindow, () => updater.check(true));
	handle('desktop:update-download', fromMainWindow, () => updater.download());
	handle('desktop:update-install', fromMainWindow, () => updater.install());

	// Bundled status page only: raw logs. No database configuration IPC exists.
	handle('desktop:worker-tail', fromBundledPage, () => worker.tail());
}

function isTitleBarTheme(value: unknown): value is TitleBarTheme {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	const hex = /^#[0-9a-fA-F]{6}$/;
	return typeof v.background === 'string' && hex.test(v.background) && typeof v.symbol === 'string' && hex.test(v.symbol);
}

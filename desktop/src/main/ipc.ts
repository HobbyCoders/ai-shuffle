/**
 * IPC handlers behind the preload bridge.
 *
 * Every handler checks that the message comes from the main window. The
 * worker's raw log is additionally limited to the bundled status page
 * (file://). No renderer can read or change database credentials.
 */
import { app, ipcMain, shell, type IpcMainEvent, type IpcMainInvokeEvent } from 'electron';
import type { PostgresPhase } from './postgres';
import type { Updater } from './updater';
import { managedContext } from './managed-context';
import type { MainWindow, TitleBarTheme } from './window';
import type { WorkerState } from './worker';
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
	state(): DesktopState;
	/** The supervisor lines followed by the worker's, for the status page log. */
	tail(): string[];
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
		event.returnValue = {
			platform: process.platform,
			version: managedContext() ? `main@${managedContext()!.application.revision.slice(0, 12)}` : app.getVersion(),
			agentAccess: AGENT_ACCESS
		};
	});

	// Window and shell.
	handle('desktop:window-action', fromMainWindow, (_e, action) => {
		if (action === 'close') window.browserWindow?.close();
	});
	handle('desktop:window-state', fromMainWindow, () => window.windowState());
	handle('desktop:set-title-bar-theme', fromMainWindow, (_e, theme) => {
		if (!isTitleBarTheme(theme)) return;
		window.setTitleBarTheme(theme);
	});

	// Worker state is visible to every page; control is too, since the web UI
	// is the worker's own and restarting it is not a secret.
	handle('desktop:worker-state', fromMainWindow, () => worker.state());
	handle('desktop:worker-restart', fromMainWindow, () => worker.restart());
	handle('desktop:worker-open-logs', fromMainWindow, () => void shell.showItemInFolder(worker.state().logPath));

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

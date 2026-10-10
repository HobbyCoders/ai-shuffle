/**
 * IPC handlers behind the preload bridge.
 *
 * Every handler checks that the message comes from the main window's main
 * frame AND that the document in it is one the window trusts: the bundled
 * status page (that exact file) or the connected worker's origin. A page the
 * window was redirected or navigated to by mistake gets nothing. Worker
 * control (restart, the log folder, the raw log, the way back to the worker
 * UI) is limited to the bundled status page, the only page the preload
 * exposes it to. No renderer can read or change database credentials.
 */
import { app, ipcMain, shell, type IpcMainEvent, type IpcMainInvokeEvent } from 'electron';
import { trustedPage, type TrustedPage } from './pages';
import type { PostgresPhase } from './postgres';
import type { Updater } from './updater';
import { managedContext } from './managed-context';
import type { MainWindow, TitleBarTheme } from './window';
import type { WorkerState } from './worker';
import { AGENT_ACCESS } from './disclosure';
import type { CallPopout } from './call-popout';

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
	/** Leave the status page for the worker UI when the worker is ready; otherwise nothing. */
	openUi(): Promise<void>;
}

interface Deps {
	window: Pick<MainWindow, 'isOwnSender' | 'currentWorkerUrl' | 'bundledPageUrl' | 'browserWindow' | 'windowState' | 'setTitleBarTheme'>;
	updater: Updater;
	worker: WorkerControl;
	/** The voice call pop-out; it validates the state it is given. */
	callPopout?: Pick<CallPopout, 'open' | 'update' | 'close'>;
}

type Handler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;

export function registerIpc({ window, updater, worker, callPopout }: Deps): void {
	const senderPage = (event: IpcMainInvokeEvent | IpcMainEvent): TrustedPage | null => {
		if (!window.isOwnSender(event.sender.id)) return null;
		const frame = event.senderFrame;
		const main = event.sender.mainFrame;
		if (!frame || !main) return null;
		if (frame !== main && (frame.processId !== main.processId || frame.routingId !== main.routingId)) return null;
		return trustedPage(frame.url, window.currentWorkerUrl, window.bundledPageUrl);
	};
	const fromTrustedPage = (event: IpcMainInvokeEvent | IpcMainEvent) => senderPage(event) !== null;
	const fromBundledPage = (event: IpcMainInvokeEvent | IpcMainEvent) => senderPage(event) === 'bundled';
	const fromWorkerPage = (event: IpcMainInvokeEvent | IpcMainEvent) => senderPage(event) === 'worker';

	const handle = (channel: string, guard: (event: IpcMainInvokeEvent) => boolean, fn: Handler) => {
		ipcMain.handle(channel, (event, ...args) => {
			if (!guard(event)) throw new Error(`${channel}: not allowed from this page`);
			return fn(event, ...args);
		});
	};

	// Synchronous so the bridge can expose plain properties at preload time.
	ipcMain.on('desktop:info', (event) => {
		if (!fromTrustedPage(event)) {
			event.returnValue = null;
			return;
		}
		event.returnValue = {
			platform: process.platform,
			version: managedContext() ? `main@${managedContext()!.application.revision.slice(0, 12)}` : app.getVersion(),
			agentAccess: AGENT_ACCESS
		};
	});

	// Window and shell. `close` is not used by the web UI; the desktop CI
	// verification scripts (scripts/verify-*.mjs) close the app through it.
	handle('desktop:window-action', fromTrustedPage, (_e, action) => {
		if (action === 'close') window.browserWindow?.close();
	});
	handle('desktop:window-state', fromTrustedPage, () => window.windowState());
	handle('desktop:set-title-bar-theme', fromTrustedPage, (_e, theme) => {
		if (!isTitleBarTheme(theme)) return;
		window.setTitleBarTheme(theme);
	});

	// Worker state (both trusted pages read it; it includes the local log path).
	handle('desktop:worker-state', fromTrustedPage, () => worker.state());

	// Updates. The web UI drives check and download through the worker's
	// update bridge (updater.ts); `check` stays for the CI verification scripts.
	handle('desktop:update-state', fromTrustedPage, () => updater.current);
	handle('desktop:update-check', fromTrustedPage, () => updater.check(true));
	handle('desktop:update-install', fromTrustedPage, () => updater.install());

	// Bundled status page only: worker control and raw logs. No database configuration IPC exists.
	handle('desktop:worker-restart', fromBundledPage, () => worker.restart());
	handle('desktop:worker-open-logs', fromBundledPage, () => void shell.showItemInFolder(worker.state().logPath));
	handle('desktop:worker-tail', fromBundledPage, () => worker.tail());
	handle('desktop:worker-open-ui', fromBundledPage, () => worker.openUi());

	// Worker UI only (voice calls happen there): the call pop-out. The page
	// keeps the call; the pop-out shows the state it sends (call-popout.ts).
	handle('desktop:call-popout-open', fromWorkerPage, (_e, state) => callPopout?.open(state) ?? false);
	handle('desktop:call-popout-update', fromWorkerPage, (_e, state) => void callPopout?.update(state));
	handle('desktop:call-popout-close', fromWorkerPage, () => void callPopout?.close());
}

function isTitleBarTheme(value: unknown): value is TitleBarTheme {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	const hex = /^#[0-9a-fA-F]{6}$/;
	return typeof v.background === 'string' && hex.test(v.background) && typeof v.symbol === 'string' && hex.test(v.symbol);
}

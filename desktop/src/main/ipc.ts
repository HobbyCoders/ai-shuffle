/**
 * IPC handlers behind the preload bridge.
 *
 * Every handler checks that the message comes from the main window. Worker
 * management (list / add / remove / connect) is additionally limited to the
 * bundled connect screen, so a worker's web UI can steer its own window but
 * never read or change which other workers this computer knows about.
 */
import { app, ipcMain, Notification, type IpcMainEvent, type IpcMainInvokeEvent } from 'electron';
import { detectLocal, probe } from './probe';
import type { ServerStore } from './servers';
import { parseServerInput } from './servers';
import type { Updater } from './updater';
import { MainWindow, TITLE_BAR_HEIGHT, type TitleBarTheme } from './window';

interface Deps {
	window: MainWindow;
	servers: ServerStore;
	updater: Updater;
}

type Handler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;

export function registerIpc({ window, servers, updater }: Deps): void {
	const fromMainWindow = (event: IpcMainInvokeEvent | IpcMainEvent) => window.isOwnSender(event.sender.id);
	const fromConnectScreen = (event: IpcMainInvokeEvent | IpcMainEvent) =>
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
		const server = window.server;
		event.returnValue = {
			platform: process.platform,
			version: app.getVersion(),
			titleBarHeight: TITLE_BAR_HEIGHT,
			server: server ? { id: server.id, name: server.name, url: server.url } : null,
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
			// Windows has no numeric badge API in Electron; the overlay icon is
			// the closest thing and is handled by the page-driven flash instead.
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
	handle('desktop:switch-worker', fromMainWindow, async () => {
		await servers.clearLast();
		await window.showConnect();
	});

	// Updates.
	handle('desktop:update-state', fromMainWindow, () => updater.current);
	handle('desktop:update-check', fromMainWindow, () => updater.check());
	handle('desktop:update-install', fromMainWindow, () => updater.install());

	// Connect screen only.
	handle('desktop:servers-list', fromConnectScreen, () => servers.list());
	handle('desktop:servers-add', fromConnectScreen, async (_e, input) => {
		const { url, name } = (input ?? {}) as { url?: unknown; name?: unknown };
		if (typeof url !== 'string') throw new Error('Enter the worker address.');
		const parsed = parseServerInput(url);
		const server = await servers.add({ url: parsed.origin, name: typeof name === 'string' ? name : undefined });
		return { server, pairingCode: parsed.pairingCode };
	});
	handle('desktop:servers-rename', fromConnectScreen, async (_e, id, name) => {
		if (typeof id === 'string' && typeof name === 'string') await servers.rename(id, name);
	});
	handle('desktop:servers-remove', fromConnectScreen, async (_e, id) => {
		if (typeof id === 'string') await servers.remove(id);
	});
	handle('desktop:servers-connect', fromConnectScreen, async (_e, id, pairingCode) => {
		if (typeof id !== 'string') return;
		const server = servers.get(id);
		if (!server) throw new Error('That worker is no longer saved.');
		await servers.markUsed(server.id);
		const target =
			typeof pairingCode === 'string' && /^[A-Z0-9]{12}$/.test(pairingCode)
				? { ...server, url: `${server.url}/pair#token=${pairingCode}` }
				: server;
		// loadServer keeps `server.url` as the origin for the navigation policy;
		// only the first navigation carries the pairing fragment.
		await window.loadServer(server);
		if (target !== server) await window.browserWindow?.loadURL(target.url);
	});
	handle('desktop:servers-probe', fromConnectScreen, async (_e, url) => {
		if (typeof url !== 'string') return null;
		return probe(parseServerInput(url).origin);
	});
	handle('desktop:servers-detect-local', fromConnectScreen, () => detectLocal());
}

function isTitleBarTheme(value: unknown): value is TitleBarTheme {
	if (!value || typeof value !== 'object') return false;
	const v = value as Record<string, unknown>;
	const hex = /^#[0-9a-fA-F]{6}$/;
	return typeof v.background === 'string' && hex.test(v.background) && typeof v.symbol === 'string' && hex.test(v.symbol);
}

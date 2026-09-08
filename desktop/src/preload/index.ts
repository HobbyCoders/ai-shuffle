/**
 * `window.shuffleDesktop`: the bridge the web UI uses to notice it runs in
 * the desktop app and to talk to the shell.
 *
 * The preload runs for every document in the main window. The worker's web
 * UI gets the window, theme, notification, update and worker-status surface.
 * The database setup and the raw worker log are exposed only to the bundled
 * setup/status page (file://) as `window.shuffleDesktopWorker`.
 */
import { contextBridge, ipcRenderer } from 'electron';

type Listener<T> = (payload: T) => void;

function subscribe<T>(channel: string, listener: Listener<T>): () => void {
	const wrapped = (_event: unknown, payload: T) => listener(payload);
	ipcRenderer.on(channel, wrapped);
	return () => {
		ipcRenderer.removeListener(channel, wrapped);
	};
}

const info = ipcRenderer.sendSync('desktop:info') as {
	platform: NodeJS.Platform;
	version: string;
	titleBarHeight: number;
	worker: { url: string; phase: string; external: boolean } | null;
	windowState: { maximized: boolean; fullscreen: boolean; focused: boolean };
	updateState: unknown;
} | null;

const bridge = {
	/** Shell version, e.g. `0.8.5`. */
	version: info?.version ?? '0.0.0',
	platform: info?.platform ?? process.platform,
	titleBarHeight: info?.titleBarHeight ?? 52,

	window: {
		getState: () => ipcRenderer.invoke('desktop:window-state') as Promise<{ maximized: boolean; fullscreen: boolean; focused: boolean }>,
		onStateChange: (listener: Listener<{ maximized: boolean; fullscreen: boolean; focused: boolean }>) => subscribe('desktop:window-state', listener),
		minimize: () => void ipcRenderer.invoke('desktop:window-action', 'minimize'),
		toggleMaximize: () => void ipcRenderer.invoke('desktop:window-action', 'toggle-maximize'),
		close: () => void ipcRenderer.invoke('desktop:window-action', 'close'),
		focus: () => void ipcRenderer.invoke('desktop:window-action', 'focus'),
		/** Colours for the OS-drawn window controls; hex `#rrggbb` only. */
		setTitleBarTheme: (theme: { background: string; symbol: string }) => void ipcRenderer.invoke('desktop:set-title-bar-theme', theme)
	},

	shell: {
		openExternal: (url: string) => void ipcRenderer.invoke('desktop:open-external', url),
		setBadge: (count: number) => void ipcRenderer.invoke('desktop:set-badge', count),
		notify: (payload: { title: string; body?: string; tag?: string; silent?: boolean }) => void ipcRenderer.invoke('desktop:notify', payload),
		onNotificationClick: (listener: Listener<{ tag: string | null }>) => subscribe('desktop:notification-click', listener),
		/** Fired for menu items and tray actions: `new-session`, `command-palette`, `toggle-sidebar`, `open-settings`, `focus-composer`, `find`, `open-docs`. */
		onCommand: (listener: Listener<string>) => subscribe('desktop:command', listener),
		onNativeTheme: (listener: Listener<{ dark: boolean }>) => subscribe('desktop:native-theme', listener)
	},

	/** The local worker this window belongs to. */
	worker: {
		getState: () => ipcRenderer.invoke('desktop:worker-state'),
		onState: (listener: Listener<unknown>) => subscribe('desktop:worker-state', listener),
		restart: () => ipcRenderer.invoke('desktop:worker-restart'),
		openLogs: () => void ipcRenderer.invoke('desktop:worker-open-logs'),
		openDatabaseSettings: () => void ipcRenderer.invoke('desktop:worker-open-setup')
	},

	updates: {
		getState: () => ipcRenderer.invoke('desktop:update-state'),
		onState: (listener: Listener<unknown>) => subscribe('desktop:update-state', listener),
		check: () => ipcRenderer.invoke('desktop:update-check'),
		install: () => void ipcRenderer.invoke('desktop:update-install')
	}
};

contextBridge.exposeInMainWorld('shuffleDesktop', bridge);

if (window.location.protocol === 'file:') {
	contextBridge.exposeInMainWorld('shuffleDesktopWorker', {
		getSettings: () => ipcRenderer.invoke('desktop:settings-get'),
		checkDatabase: (config: unknown, action: 'test' | 'create') => ipcRenderer.invoke('desktop:db-check', config, action),
		saveAndStart: (config: unknown) => ipcRenderer.invoke('desktop:setup-save', config),
		cancelSetup: () => void ipcRenderer.invoke('desktop:setup-cancel'),
		openSetup: () => void ipcRenderer.invoke('desktop:worker-open-setup'),
		getState: () => ipcRenderer.invoke('desktop:worker-state'),
		onState: (listener: Listener<unknown>) => subscribe('desktop:worker-state', listener),
		onLog: (listener: Listener<string>) => subscribe('desktop:worker-log', listener),
		tail: () => ipcRenderer.invoke('desktop:worker-tail') as Promise<string[]>,
		restart: () => ipcRenderer.invoke('desktop:worker-restart'),
		openLogs: () => void ipcRenderer.invoke('desktop:worker-open-logs')
	});
}

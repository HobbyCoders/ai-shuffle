/**
 * `window.shuffleDesktop`: the bridge the web UI uses to notice it runs in
 * the desktop app and to talk to the shell.
 *
 * The preload runs for every document in the main window. The worker's web
 * UI gets the window, menu-command, update and worker-status surface.
 * The raw worker log and the way back to the worker UI are exposed only to
 * the bundled status page (file://) as `window.shuffleDesktopWorker`. No database configuration is exposed.
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
	agentAccess?: { title: string; message: string; points: string[]; footer: string };
} | null;

const bridge = {
	/** Shell version, e.g. `0.8.5`. */
	version: info?.version ?? '0.0.0',
	platform: info?.platform ?? process.platform,
	/** "Agents run as you": the disclosure shown on first launch, for Settings to repeat. */
	agentAccess: info?.agentAccess ?? null,

	window: {
		getState: () => ipcRenderer.invoke('desktop:window-state') as Promise<{ maximized: boolean; fullscreen: boolean; focused: boolean }>,
		onStateChange: (listener: Listener<{ maximized: boolean; fullscreen: boolean; focused: boolean }>) => subscribe('desktop:window-state', listener),
		close: () => void ipcRenderer.invoke('desktop:window-action', 'close'),
		/** Colours for the OS-drawn window controls; hex `#rrggbb` only. */
		setTitleBarTheme: (theme: { background: string; symbol: string }) => void ipcRenderer.invoke('desktop:set-title-bar-theme', theme)
	},

	shell: {
		/** Fired for menu items and tray actions: `new-session`, `command-palette`, `toggle-sidebar`, `open-settings`, `focus-composer`, `find`, `open-docs`. */
		onCommand: (listener: Listener<string>) => subscribe('desktop:command', listener)
	},

	/** The local worker this window belongs to. */
	worker: {
		getState: () => ipcRenderer.invoke('desktop:worker-state')
	},

	updates: {
		getState: () => ipcRenderer.invoke('desktop:update-state'),
		onState: (listener: Listener<unknown>) => subscribe('desktop:update-state', listener),
		check: () => ipcRenderer.invoke('desktop:update-check'),
		download: () => ipcRenderer.invoke('desktop:update-download') as Promise<void>,
		install: () => ipcRenderer.invoke('desktop:update-install') as Promise<void>
	}
};

contextBridge.exposeInMainWorld('shuffleDesktop', bridge);

if (window.location.protocol === 'file:') {
	contextBridge.exposeInMainWorld('shuffleDesktopWorker', {
		getState: () => ipcRenderer.invoke('desktop:worker-state'),
		onState: (listener: Listener<unknown>) => subscribe('desktop:worker-state', listener),
		onLog: (listener: Listener<string>) => subscribe('desktop:worker-log', listener),
		tail: () => ipcRenderer.invoke('desktop:worker-tail') as Promise<string[]>,
		restart: () => ipcRenderer.invoke('desktop:worker-restart'),
		openLogs: () => void ipcRenderer.invoke('desktop:worker-open-logs'),
		/** Back to the worker UI once the worker is ready; a no-op otherwise. */
		openUi: () => ipcRenderer.invoke('desktop:worker-open-ui') as Promise<void>
	});
}

/**
 * Desktop app integration.
 *
 * The desktop shell (desktop/) is an Electron window that loads this UI from
 * the worker's real origin and injects `window.shuffleDesktop` through its
 * preload. The window is frameless with the OS window controls overlaid, so
 * when the bridge is present:
 *
 * - `<html>` gets `desktop` and `desktop-<platform>` classes; layout CSS uses
 *   them to make the top strip draggable and to keep controls out from under
 *   the OS buttons (see `env(titlebar-area-*)` in app.css).
 * - The shell's window-control colours follow the resolved theme.
 * - Native menu commands (⌘N, ⌘K, ⌘B, ⌘,) run the same actions as the
 *   in-page shortcuts, so both paths stay in step.
 *
 * Everything here is inert in a browser: `desktop` is null and `initDesktop`
 * returns a no-op.
 */
import { browser } from '$app/environment';

// The stores this module drives are imported lazily inside initDesktop():
// importing this file must stay side-effect free, because components such as
// StudioPageHeader read `desktop` in environments (unit tests) where the
// theme store's module-time `window.matchMedia` would throw.

export type DesktopPlatform = 'win32' | 'darwin' | 'linux';

export interface DesktopWindowState {
	maximized: boolean;
	fullscreen: boolean;
	focused: boolean;
}

export type DesktopUpdatePhase = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error' | 'unsupported';

export interface DesktopUpdateState {
	phase: DesktopUpdatePhase;
	version: string | null;
	percent: number | null;
	error: string | null;
	checkedAt: string | null;
	message?: string | null;
}

export interface DesktopWorkerState {
	phase: 'idle' | 'starting' | 'ready' | 'stopping' | 'stopped' | 'failed';
	port: number;
	url: string;
	pid: number | null;
	external: boolean;
	error: string | null;
	exitCode: number | null;
	startedAt: string | null;
	logPath: string;
}

export type DesktopCommand =
	| 'new-session'
	| 'command-palette'
	| 'toggle-sidebar'
	| 'open-settings'
	| 'focus-composer'
	| 'find'
	| 'open-docs';

export interface ShuffleDesktopBridge {
	version: string;
	platform: DesktopPlatform;
	titleBarHeight: number;
	window: {
		getState(): Promise<DesktopWindowState>;
		onStateChange(listener: (state: DesktopWindowState) => void): () => void;
		minimize(): void;
		toggleMaximize(): void;
		close(): void;
		focus(): void;
		setTitleBarTheme(theme: { background: string; symbol: string }): void;
	};
	shell: {
		openExternal(url: string): void;
		setBadge(count: number): void;
		notify(payload: { title: string; body?: string; tag?: string; silent?: boolean }): void;
		onNotificationClick(listener: (payload: { tag: string | null }) => void): () => void;
		onCommand(listener: (command: DesktopCommand | string) => void): () => void;
		onNativeTheme(listener: (payload: { dark: boolean }) => void): () => void;
	};
	/** The local worker this window belongs to. */
	worker: {
		getState(): Promise<DesktopWorkerState>;
		onState(listener: (state: DesktopWorkerState) => void): () => void;
		restart(): Promise<void>;
		openLogs(): void;
		openStatus(): void;
	};
	updates: {
		getState(): Promise<DesktopUpdateState>;
		onState(listener: (state: DesktopUpdateState) => void): () => void;
		check(): Promise<void>;
		download(): Promise<void>;
		install(): Promise<void>;
	};
}

declare global {
	interface Window {
		shuffleDesktop?: ShuffleDesktopBridge;
	}
}

/** The bridge, or null outside the desktop app. Only the top-level document is trusted. */
export const desktop: ShuffleDesktopBridge | null =
	browser && window === window.top && window.shuffleDesktop ? window.shuffleDesktop : null;

export const isDesktop = desktop !== null;

/** Command handlers the page registers; the menu looks them up by name. */
const commandHandlers = new Map<string, () => void>();

/**
 * Lets a page own a menu command. The default handler (below) already routes
 * commands to registered keyboard shortcuts, so most pages need nothing.
 */
export function onDesktopCommand(command: DesktopCommand, handler: () => void): () => void {
	commandHandlers.set(command, handler);
	return () => {
		if (commandHandlers.get(command) === handler) commandHandlers.delete(command);
	};
}

const SHORTCUT_FOR_COMMAND: Record<string, string> = {
	'new-session': 'new-chat',
	'command-palette': 'spotlight',
	'open-settings': 'open-settings',
	find: 'advanced-search'
};

async function runCommand(command: string): Promise<void> {
	const own = commandHandlers.get(command);
	if (own) {
		own();
		return;
	}
	if (command === 'toggle-sidebar') {
		const { layout } = await import('$lib/stores/layout.svelte');
		layout.toggleSidebar();
		return;
	}
	if (command === 'focus-composer') {
		const editor = document.querySelector<HTMLElement>('[data-composer-editor], .ProseMirror');
		editor?.focus();
		return;
	}
	if (command === 'open-docs') {
		const { layout } = await import('$lib/stores/layout.svelte');
		layout.openFeaturePage('docs');
		return;
	}
	const shortcutId = SHORTCUT_FOR_COMMAND[command];
	if (!shortcutId) return;
	const { getShortcuts } = await import('$lib/services/keyboard');
	getShortcuts()
		.find((shortcut) => shortcut.id === shortcutId)
		?.action();
}

/**
 * Colours for the OS-drawn window controls. The tokens are oklch, which the
 * shell cannot parse, so they are resolved to hex through a canvas.
 */
function resolveHex(cssColor: string, fallback: string): string {
	try {
		const canvas = document.createElement('canvas');
		canvas.width = canvas.height = 1;
		const ctx = canvas.getContext('2d', { willReadFrequently: true });
		if (!ctx) return fallback;
		ctx.fillStyle = cssColor;
		ctx.fillRect(0, 0, 1, 1);
		const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
		return `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}`;
	} catch {
		return fallback;
	}
}

/**
 * A downloaded update is announced once, in the bell, with a button that
 * restarts into it. The item is local to this window (not persisted to the
 * worker), because it is about this computer's app, not the worker.
 */
let announcedUpdateVersion: string | null = null;

async function announceUpdate(state: DesktopUpdateState): Promise<void> {
	if (!desktop || state.phase !== 'ready') return;
	const version = state.version ?? 'new';
	if (announcedUpdateVersion === version) return;
	announcedUpdateVersion = version;
	const { notify } = await import('$lib/stores/notifications');
	const shell = desktop;
	notify.info(`AI Shuffle Desktop ${version} has downloaded. Restart to finish installing it.`, {
		title: 'Update ready',
		category: 'desktop-update',
		persist: false,
		action: { label: 'Restart and update', onClick: () => shell.updates.install() }
	});
}

function syncTitleBarTheme(): void {
	if (!desktop) return;
	const styles = getComputedStyle(document.documentElement);
	const dark = document.documentElement.classList.contains('dark');
	const background = resolveHex(styles.getPropertyValue('--sidebar').trim() || (dark ? '#000000' : '#ffffff'), dark ? '#000000' : '#ffffff');
	const symbol = resolveHex(styles.getPropertyValue('--foreground').trim() || (dark ? '#e9e9ec' : '#1d1d1f'), dark ? '#e9e9ec' : '#1d1d1f');
	desktop.window.setTitleBarTheme({ background, symbol });
}

/** Wires the bridge for the lifetime of the app. Returns a cleanup. */
export function initDesktop(): () => void {
	if (!desktop) return () => {};
	const root = document.documentElement;
	root.classList.add('desktop', `desktop-${desktop.platform}`);

	const applyWindowState = (state: DesktopWindowState) => {
		root.classList.toggle('desktop-maximized', state.maximized);
		root.classList.toggle('desktop-fullscreen', state.fullscreen);
		root.classList.toggle('desktop-blurred', !state.focused);
	};
	void desktop.window.getState().then(applyWindowState);
	const offState = desktop.window.onStateChange(applyWindowState);
	const offCommand = desktop.shell.onCommand((command) => void runCommand(command));
	void desktop.updates.getState().then((state) => void announceUpdate(state));
	const offUpdates = desktop.updates.onState((state) => void announceUpdate(state));

	// Tokens settle after the theme store applies its class and preset styles;
	// re-sync whenever the theme changes.
	syncTitleBarTheme();
	let unsubscribeTheme: (() => void) | null = null;
	let disposed = false;
	void import('$lib/stores/theme.svelte')
		.then(({ theme }) => {
			if (disposed) return;
			unsubscribeTheme = theme.subscribe(() => requestAnimationFrame(syncTitleBarTheme));
		})
		.catch(() => {
			/* Without the store the controls keep the colours set above. */
		});

	return () => {
		disposed = true;
		offState();
		offCommand();
		offUpdates();
		unsubscribeTheme?.();
	};
}

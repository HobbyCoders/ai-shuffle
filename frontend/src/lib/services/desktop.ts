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
	/** Release name and notes from the update's signed manifest; absent on older desktop builds. */
	release?: string | null;
	notes?: string | null;
	/** macOS and Linux: the verified installer the shell just saved; absent on older desktop builds. */
	installer?: string | null;
}

/** Release notes flattened to one short line for a notification. */
export function notesSummary(notes: string | null | undefined, limit = 280): string {
	const text = (notes ?? '').replace(/\s+/g, ' ').trim();
	return text.length > limit ? `${text.slice(0, limit - 1).trimEnd()}…` : text;
}

export type DesktopCommand =
	| 'new-session'
	| 'command-palette'
	| 'toggle-sidebar'
	| 'open-settings'
	| 'open-updates'
	| 'focus-composer'
	| 'find'
	| 'open-docs';

/** The desktop app's "agents run as you" disclosure, shown on first launch. */
export interface DesktopAgentAccess {
	title: string;
	message: string;
	points: string[];
	footer: string;
}

/** What the popped-out call window shows (desktop/src/main/call-popout.ts validates it). */
export interface DesktopCallState {
	/** "Voice call · live", or the microphone problem. */
	label: string;
	/** The owner session's title. */
	title: string;
	micMuted: boolean;
	agentMuted: boolean;
	micProblem: boolean;
}

/** Buttons pressed in the popped-out call window, run by this page against its own call. */
export type DesktopCallCommand = 'toggle-mic' | 'toggle-agent' | 'hangup' | 'return';

/**
 * The part of the preload bridge (desktop/src/preload/index.ts) the web UI
 * uses. The preload has a few more members for the desktop CI scripts.
 */
export interface ShuffleDesktopBridge {
	version: string;
	platform: DesktopPlatform;
	/** Older shells lack it. */
	agentAccess?: DesktopAgentAccess | null;
	window: {
		getState(): Promise<DesktopWindowState>;
		onStateChange(listener: (state: DesktopWindowState) => void): () => void;
		setTitleBarTheme(theme: { background: string; symbol: string }): void;
	};
	shell: {
		onCommand(listener: (command: DesktopCommand | string) => void): () => void;
	};
	/**
	 * Checking and downloading go through the worker's update bridge (the
	 * shell polls the worker for requests), so the page only reads the state
	 * and asks for the install.
	 */
	updates: {
		getState(): Promise<DesktopUpdateState>;
		onState(listener: (state: DesktopUpdateState) => void): () => void;
		install(): Promise<void>;
	};
	/**
	 * The away call bar in its own small always-on-top window, so the call
	 * controls stay visible over other apps. This page keeps the call and its
	 * audio; the window only shows `DesktopCallState` and sends commands back.
	 * Older shells lack it.
	 */
	voiceCall?: {
		/** Opens (or refreshes) the window; false when the shell refused. */
		open(state: DesktopCallState): Promise<boolean>;
		update(state: DesktopCallState): void;
		close(): void;
		onCommand(listener: (command: DesktopCallCommand) => void): () => void;
		/** The window closed: its dock button, or the shell closed it. */
		onClosed(listener: () => void): () => void;
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

const SHORTCUT_FOR_COMMAND: Record<string, string> = {
	'new-session': 'new-chat',
	'command-palette': 'spotlight',
	'open-settings': 'open-settings',
	find: 'advanced-search'
};

/** Menu and tray commands are routed to the matching keyboard shortcut or store action. */
async function runCommand(command: string): Promise<void> {
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
	if (command === 'open-updates') {
		// The shell's "Check for Updates…" lands on the in-app Updates section.
		const { openSettings } = await import('$lib/settings/navigation.svelte');
		openSettings({ category: 'system', anchor: 'system.checkUpdates' });
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
 * A downloaded desktop update is announced once in this window's bell.
 * Its action enters the shared worker-update control, which prepares and
 * installs according to the current account scope, not the shell alone.
 */
let announcedUpdateVersion: string | null = null;
let announcedInstaller: string | null = null;
let announcedFailure: string | null = null;

async function announceUpdate(state: DesktopUpdateState): Promise<void> {
	if (!desktop) return;
	// macOS and Linux: a downloaded installer is run by the owner; say where it went.
	if (state.installer && state.installer !== announcedInstaller) {
		announcedInstaller = state.installer;
		const { notify } = await import('$lib/stores/notifications');
		notify.info(state.message || `The installer was downloaded and verified. Open ${state.installer} to install it.`, {
			title: 'Installer downloaded', category: 'desktop-update', persist: false
		});
		return;
	}
	if (state.phase !== 'ready') return;
	const version = state.version ?? 'new';
	if (state.error) {
		// A staged update that did not install (sessions still running, or the
		// install failed). It stays staged, so the shared control can retry it.
		const key = `${version}\n${state.error}`;
		if (announcedFailure === key) return;
		announcedFailure = key;
		announcedUpdateVersion = version;
		const { notify } = await import('$lib/stores/notifications');
		notify.warning(state.error, {
			title: 'Update not installed',
			category: 'desktop-update',
			persist: false,
			action: { label: 'Try again', onClick: () => window.dispatchEvent(new Event('application-update-request')) }
		});
		return;
	}
	announcedFailure = null;
	if (announcedUpdateVersion === version) return;
	announcedUpdateVersion = version;
	const { notify } = await import('$lib/stores/notifications');
	const summary = notesSummary(state.notes);
	notify.info(`AI Shuffle Desktop ${state.release || version} has downloaded. Restart to finish installing it.${summary ? ` What's new: ${summary}` : ''}`, {
		title: 'Update ready',
		category: 'desktop-update',
		persist: false,
		// The shared control owns scope, preparation, receipts and restart consent.
		action: { label: 'Restart and update', onClick: () => window.dispatchEvent(new Event('application-update-request')) }
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

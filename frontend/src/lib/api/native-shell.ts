/**
 * Messages from the phone UI to the Android app's shell over its
 * `shuffleShell` web-message bridge (android/.../ui/MobileUi.kt
 * `SHELL_BRIDGE`), scoped like `shuffleFileSave` to the top-level phone UI
 * page on the paired worker.
 *
 * The app draws its own worker-name bar until the page says it draws the
 * "Connected to" bar itself, so a worker UI from before that bar keeps the
 * app's. The bar's sheet then reaches the app's Workers screen (pair,
 * rename, remove) through `openNativeWorkers`, which the app's bar menu
 * used to offer, and the app's own Settings (account, sign out, phone tools)
 * through `openNativeAppSettings`.
 *
 * The bridge object exists only in app builds that install the listener;
 * everywhere else these calls do nothing and report false.
 */
import { get } from 'svelte/store';
import { isBundledMobile, nativeCapability } from '$lib/api/runtime';
import { hangup, isCallActiveFor, voiceCallState } from '$lib/services/voiceCall';

interface ShellBridge {
	postMessage(message: string): void;
}

function shellBridge(): ShellBridge | null {
	if (typeof window === 'undefined' || !isBundledMobile()) return null;
	const bridge = (window as unknown as { shuffleShell?: Partial<ShellBridge> }).shuffleShell;
	return bridge && typeof bridge.postMessage === 'function' ? (bridge as ShellBridge) : null;
}

function post(message: Record<string, unknown>): boolean {
	const bridge = shellBridge();
	if (!bridge) return false;
	try {
		bridge.postMessage(JSON.stringify(message));
		return true;
	} catch {
		return false;
	}
}

/** Whether the app showing this page takes shell messages. */
export function hasShellBridge(): boolean {
	return shellBridge() !== null;
}

/** Tell the app whether this page shows its own "Connected to" bar. */
export function announceWorkerBar(shown: boolean): boolean {
	return post({ type: 'worker-bar', shown });
}

/**
 * Open the app's native Workers screen. The app opens it with CLEAR_TOP, which
 * closes the chat screen and with it any voice call, so a live call is ended
 * only after the user agrees. Returns true when handled (opened or declined).
 */
export function openNativeWorkers(): boolean {
	if (!hasShellBridge()) return false;
	const call = get(voiceCallState);
	if (isCallActiveFor(call.sessionId)) {
		if (!window.confirm('Opening Workers ends the current voice call. Continue?')) return true;
		// Let the hangup reach the worker before the app tears the page down,
		// but never leave Workers unreachable behind a slow or failing hangup.
		const settled = Promise.resolve(hangup('user')).catch(() => undefined);
		const capped = new Promise((resolve) => setTimeout(resolve, HANGUP_WAIT_MS));
		void Promise.race([settled, capped]).then(() => post({ type: 'workers' }));
		return true;
	}
	return post({ type: 'workers' });
}

/** Longest wait for a hangup before opening Workers anyway. */
export const HANGUP_WAIT_MS = 3000;

/** The app opens its own Settings (account, sign out, phone tools) on `{"type":"app-settings"}`. */
export const APP_SETTINGS_CAPABILITY = 'app-settings';

/**
 * Where "App settings" lands in the app showing this page: `settings` when it
 * opens its own Settings screen directly, `workers` on older builds whose
 * Workers screen has a Settings button, or null outside the app. Keep this
 * fallback until a minimum app build supporting app-settings is enforced.
 * Workers uses CLEAR_TOP, so this legacy route cannot preserve chat's Back stack.
 */
export function nativeAppSettingsRoute(): 'settings' | 'workers' | null {
	if (!hasShellBridge()) return null;
	return nativeCapability(APP_SETTINGS_CAPABILITY) ? 'settings' : 'workers';
}

/** Open the app's own Settings screen (account, sign out, phone tools). */
export function openNativeAppSettings(): boolean {
	const route = nativeAppSettingsRoute();
	return route === 'settings' ? post({ type: 'app-settings' }) : route === 'workers' ? openNativeWorkers() : false;
}

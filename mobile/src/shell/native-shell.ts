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
 * used to offer.
 *
 * The bridge object exists only in app builds that install the listener;
 * everywhere else these calls do nothing and report false.
 */
import { isBundledMobile } from '$lib/api/runtime';

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

/** Open the app's native Workers screen. */
export function openNativeWorkers(): boolean {
	return post({ type: 'workers' });
}
